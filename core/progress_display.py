"""
Rich terminal progress display for the Migration Platform.

Auto-installs `rich` on first run via the driver_installer.
Falls back to plain text output if rich cannot be installed.
"""
from __future__ import annotations

import time
from typing import Any

from core.driver_installer import ensure_driver

# ---------------------------------------------------------------------------
# Attempt to install and import rich
# ---------------------------------------------------------------------------
try:
    ensure_driver("rich", "rich")
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH_AVAILABLE = True
except Exception:
    _RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Phase definitions (display order)
# ---------------------------------------------------------------------------
_PHASE_LABELS: list[tuple[str, str]] = [
    ("connect",            "Connect"),
    ("preflight",          "Source / Target Preflight"),
    ("ensure_database",    "Ensure Database"),
    ("extensions",         "Extensions"),
    ("schemas",            "Schemas"),
    ("custom_types",       "Custom Types"),
    ("create_sequences",   "Create Sequences"),
    ("create_tables",      "Create Tables"),
    ("create_partitions",  "Create Partitions"),
    ("data",               "Migrate Data"),
    ("apply_constraints",  "Indexes + Constraints"),
    ("row_level_security", "Row-Level Security"),
    ("advance_sequences",  "Advance Sequences"),
    ("views",              "Views"),
    ("materialized_views", "Materialized Views"),
    ("functions",          "Functions & Procs"),
    ("triggers",           "Triggers"),
    ("comments",           "Comments"),
    ("security_principals", "Security & Access"),
    ("grants",             "Grants"),
    ("validation",         "Validation"),
]
_PHASE_KEYS = [k for k, _ in _PHASE_LABELS]

_SCHEMA_STATUS_LABELS = {
    "COMPATIBLE": "Compatible",
    "TARGET_MISSING": "Target Missing",
    "MISMATCH": "Schema Mismatch",
}
_ACTION_LABELS = {
    "MIGRATE": "Update & Migrate",
    "CREATE_AND_MIGRATE": "Create & Migrate",
    "BLOCK": "Blocked",
}


def _preflight_rows(preflight: dict[str, Any]) -> list[tuple[str, str, int | str, int, str]]:
    rows: list[tuple[str, str, int | str, int, str]] = []
    for object_plan in preflight.get("objects", []):
        comparison = object_plan.get("schema_comparison") or {}
        source_rows = object_plan.get("source_row_count")
        target_rows = object_plan.get("target_row_count")
        rows.append(
            (
                object_plan.get("object_name") or "—",
                _SCHEMA_STATUS_LABELS.get(comparison.get("status"), "Unavailable"),
                source_rows if source_rows is not None else "—",
                target_rows if target_rows is not None else 0,
                _ACTION_LABELS.get(object_plan.get("action"), "Blocked"),
            )
        )
    return rows


def _preflight_blockers(preflight: dict[str, Any]) -> list[str]:
    blockers = [issue.get("message", "Preflight blocked.") for issue in preflight.get("blockers", [])]
    for object_plan in preflight.get("objects", []):
        blockers.extend(object_plan.get("blockers", []))
    return blockers


class RichProgressDisplay:
    """
    Live terminal display that integrates with the orchestrator via
    update_status() / record_table_stats() — same interface as StatusServer.
    """

    def __init__(self, run_id: str, mode: str) -> None:
        self._run_id = run_id
        self._mode = mode
        self._start_time = time.time()
        self._current_phase = "idle"
        self._progress = 0
        self._errors: list[str] = []
        self._table_stats: dict[str, dict[str, int]] = {}
        self._phases_done: list[str] = []
        self._preflight: dict[str, Any] | None = None
        self._live: Any = None
        self._console: Any = None

    # ------------------------------------------------------------------ public API
    def update_status(self, phase: str, progress: int, errors: list[str] | None = None) -> None:
        prev = self._current_phase
        if prev and prev != "idle" and prev != phase:
            base = prev.split(":")[0].strip().replace(" ", "_").lower()
            if base not in self._phases_done:
                self._phases_done.append(base)
        self._current_phase = phase
        self._progress = progress
        if errors:
            self._errors = list(errors)
        if self._live and _RICH_AVAILABLE:
            self._live.update(self._render())

    def record_table_stats(self, table: str, source_rows: int, success: int, failure: int) -> None:
        self._table_stats[table] = {"source": source_rows, "success": success, "failure": failure}
        if self._live and _RICH_AVAILABLE:
            self._live.update(self._render())

    def record_preflight(self, preflight: dict[str, Any]) -> None:
        self._preflight = preflight
        self.update_status("preflight", 3, _preflight_blockers(preflight))

    # ------------------------------------------------------------------ context manager
    def __enter__(self) -> "RichProgressDisplay":
        if _RICH_AVAILABLE:
            # force_terminal + legacy_windows=False avoids the cp1252 Win32 renderer
            # that cannot encode Unicode/emoji characters.
            self._console = Console(force_terminal=True, legacy_windows=False)
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=4,
                screen=False,
            )
            self._live.__enter__()
        else:
            print(f"[Migration Platform] Run ID: {self._run_id}  Mode: {self._mode}")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._live:
            try:
                self._live.__exit__(*args)
            except Exception:
                pass
        if _RICH_AVAILABLE and self._console:
            try:
                self._print_final_summary()
            except Exception:
                pass

    # ------------------------------------------------------------------ rendering
    def _elapsed(self) -> str:
        s = int(time.time() - self._start_time)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _render(self) -> Any:
        if not _RICH_AVAILABLE:
            return ""

        # ---- Header (ASCII-safe, no emoji for Windows cp1252 compat) ----
        elapsed = self._elapsed()
        pct = self._progress
        header_text = Text()
        header_text.append("  >> Migration Platform", style="bold bright_white")
        header_text.append(f"     Run: {self._run_id[:16]}...  ", style="dim")
        header_text.append(f"Mode: {self._mode.upper()}  ", style="cyan bold")
        header_text.append(f"Elapsed: {elapsed}", style="yellow")
        header = Panel(header_text, style="bright_black", padding=(0, 1))

        # ---- Progress bar ----
        pct_color = "green" if pct == 100 else ("yellow" if pct > 50 else "cyan")
        bar_filled = int(pct * 40 / 100)
        bar = "#" * bar_filled + "-" * (40 - bar_filled)
        progress_text = Text()
        progress_text.append(f"  |{bar}|  ", style=pct_color)
        progress_text.append(f"{pct}%  ", style=f"bold {pct_color}")
        progress_text.append("  Current: ", style="dim")
        progress_text.append(self._current_phase, style="bold white")
        err_count = len(self._errors)
        err_str = f"  ! {err_count} error{'s' if err_count != 1 else ''}" if err_count else "  OK No errors"
        err_style = "bold red" if err_count else "dim green"
        progress_text.append(f"    {err_str}", style=err_style)
        progress_panel = Panel(progress_text, title="[bold]Overall Progress[/bold]",
                               border_style="bright_black", padding=(0, 1))

        # ---- Phase checklist ----
        phase_table = Table.grid(padding=(0, 1))
        phase_table.add_column(width=1, no_wrap=True)   # icon col — must be 1 char wide
        phase_table.add_column(no_wrap=True)
        for key, label in _PHASE_LABELS:
            done = key in self._phases_done
            active = self._current_phase.lower().startswith(key) or self._current_phase.lower() == key
            if done:
                icon, style = "v", "bold green"
                lbl_style = "white"
            elif active:
                icon, style = ">", "bold cyan"
                lbl_style = "bold cyan"
            else:
                icon, style = "-", "dim"
                lbl_style = "dim"
            phase_table.add_row(Text(icon, style=style), Text(label, style=lbl_style))
        phase_panel = Panel(phase_table, title="[bold]Phases[/bold]",
                            border_style="bright_black", padding=(0, 1))

        preflight_panel = None
        if self._preflight is not None:
            preflight_rows = _preflight_rows(self._preflight)
            object_plans = self._preflight.get("objects", [])
            target_tables = sum(1 for plan in object_plans if plan.get("target_exists"))
            comparisons_complete = all(plan.get("schema_comparison") for plan in object_plans)
            row_counts_captured = all(
                plan.get("source_row_count") is not None
                and (not plan.get("target_exists") or plan.get("target_row_count") is not None)
                for plan in object_plans
            )
            preflight_table = Table(show_header=True, header_style="bold dim", box=None,
                                    padding=(0, 1), show_edge=False)
            preflight_table.add_column("Table", style="white", no_wrap=True)
            preflight_table.add_column("Schema Status")
            preflight_table.add_column("Source Rows", justify="right")
            preflight_table.add_column("Target Rows", justify="right")
            preflight_table.add_column("Migration Action")
            for table_name, schema_status, source_rows, target_rows, action in preflight_rows:
                preflight_table.add_row(
                    table_name,
                    schema_status,
                    str(source_rows),
                    str(target_rows),
                    action,
                )
            summary = (
                f"Source Connected: {'Yes' if self._preflight.get('source_connected') else 'No'}  |  "
                f"Target Connected: {'Yes' if self._preflight.get('target_connected') else 'No'}  |  "
                f"Source Tables: {len(preflight_rows)}  |  "
                f"Target Tables: {target_tables}\n"
                f"Schema Comparison: {'Complete' if comparisons_complete else 'Blocked'}  |  "
                f"Row Counts: {'Captured' if row_counts_captured else 'Blocked'}  |  "
                f"Migration Plan: {'Ready' if self._preflight.get('ready') else 'Blocked'}"
            )
            blockers = _preflight_blockers(self._preflight)
            if blockers:
                summary += "\nBlockers: " + "; ".join(blockers)
            preflight_ready = self._preflight.get("ready", False)
            preflight_panel = Panel(
                preflight_table,
                title=(
                    "[bold green]PostgreSQL Preflight Ready[/bold green]"
                    if preflight_ready
                    else "[bold red]PostgreSQL Preflight Blocked[/bold red]"
                ),
                subtitle=summary,
                border_style="green" if preflight_ready else "red",
                padding=(0, 1),
            )

        # ---- Table stats ----
        stats_table = Table(show_header=True, header_style="bold dim", box=None,
                            padding=(0, 1), show_edge=False)
        stats_table.add_column("Table", style="white", no_wrap=True)
        stats_table.add_column("Source", justify="right", style="dim")
        stats_table.add_column("Migrated", justify="right", style="green")
        stats_table.add_column("Failed", justify="right")
        stats_table.add_column("Rate", justify="right")

        if self._table_stats:
            for tname, s in self._table_stats.items():
                src = s.get("source", 0)
                suc = s.get("success", 0)
                fail = s.get("failure", 0)
                rate = f"{int(suc/src*100)}%" if src > 0 else "—"
                fail_style = "bold red" if fail > 0 else "dim"
                stats_table.add_row(
                    tname[:24],
                    f"{src:,}",
                    f"{suc:,}",
                    Text(str(fail), style=fail_style),
                    rate,
                )
        else:
            stats_table.add_row("—", "—", "—", "—", "—")

        stats_panel = Panel(stats_table, title="[bold]Table Statistics[/bold]",
                            border_style="bright_black", padding=(0, 1))

        # ---- Bottom row (two columns) ----
        from rich.columns import Columns as RColumns
        bottom = RColumns([phase_panel, stats_panel], equal=False, expand=True)

        from rich.console import Group
        renderables = [header, progress_panel]
        if preflight_panel is not None:
            renderables.append(preflight_panel)
        renderables.append(bottom)
        return Group(*renderables)

    def _print_final_summary(self) -> None:
        if not _RICH_AVAILABLE or not self._console:
            return
        total_src = sum(s.get("source", 0) for s in self._table_stats.values())
        total_suc = sum(s.get("success", 0) for s in self._table_stats.values())
        total_fail = sum(s.get("failure", 0) for s in self._table_stats.values())
        rate = f"{int(total_suc/total_src*100)}%" if total_src > 0 else "—"
        status_icon = "✓" if not self._errors else "⚠"
        status_style = "bold green" if not self._errors else "bold yellow"
        elapsed = self._elapsed()

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="dim", min_width=20)
        summary.add_column(style="bold white")
        summary.add_row("Run ID", self._run_id)
        summary.add_row("Mode", self._mode.upper())
        summary.add_row("Duration", elapsed)
        summary.add_row("Tables migrated", str(len(self._table_stats)))
        summary.add_row("Total rows", f"{total_src:,}")
        summary.add_row("Migrated", f"{total_suc:,}")
        summary.add_row("Failed", f"{total_fail:,}")
        summary.add_row("Success rate", rate)
        summary.add_row("Errors", str(len(self._errors)))
        self._console.print(
            Panel(summary, title=f"[{status_style}]{status_icon} Migration Complete[/{status_style}]",
                  border_style="green" if not self._errors else "yellow", padding=(1, 2))
        )


# ---------------------------------------------------------------------------
# Fallback: no-op display (when rich is unavailable)
# ---------------------------------------------------------------------------
class NoopProgressDisplay:
    def update_status(self, phase: str, progress: int, errors: list[str] | None = None) -> None:
        print(f"[{progress:3d}%] {phase}" + (f" ({len(errors)} errors)" if errors else ""))

    def record_table_stats(self, table: str, source_rows: int, success: int, failure: int) -> None:
        print(f"  → {table}: {success}/{source_rows} rows migrated, {failure} failed")

    def record_preflight(self, preflight: dict[str, Any]) -> None:
        print("PostgreSQL Preflight")
        for table_name, schema_status, source_rows, target_rows, action in _preflight_rows(preflight):
            print(f"  {table_name}: {schema_status}; source={source_rows}; target={target_rows}; {action}")
        for blocker in _preflight_blockers(preflight):
            print(f"  Blocked: {blocker}")

    def __enter__(self) -> "NoopProgressDisplay":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def create_progress_display(run_id: str, mode: str) -> RichProgressDisplay | NoopProgressDisplay:
    """Factory that returns the best available progress display."""
    if _RICH_AVAILABLE:
        return RichProgressDisplay(run_id, mode)
    return NoopProgressDisplay()
