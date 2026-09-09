"""
Premium Migration Report Builder.

Generates two shareable artifacts:
- {run_id}.html — self-contained HTML report (dark theme, SVG charts, accordion details)
- {run_id}.json — structured JSON report (machine-readable)
"""
from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Phase registry (all 19 phases, in execution order)
# ---------------------------------------------------------------------------
_PHASE_META: list[tuple[str, str, str]] = [
    ("connect",            "Connect",                "🔌"),
    ("ensure_database",    "Ensure Database",        "🗄"),
    ("extensions",         "Extensions",             "🧩"),
    ("schemas",            "Schemas",                "📐"),
    ("custom_types",       "Custom Types",           "🔷"),
    ("create_sequences",   "Create Sequences",       "🔢"),
    ("create_tables",      "Create Tables",          "📋"),
    ("create_partitions",  "Create Partitions",      "🗂"),
    ("data",               "Migrate Data",           "📦"),
    ("apply_constraints",  "Indexes + Constraints",  "🔗"),
    ("row_level_security", "Row-Level Security",     "🔒"),
    ("advance_sequences",  "Advance Sequences",      "⏭"),
    ("views",              "Views",                  "👁"),
    ("materialized_views", "Materialized Views",     "📸"),
    ("functions",          "Functions & Procs",      "⚙"),
    ("triggers",           "Triggers",               "⚡"),
    ("comments",           "Comments",               "💬"),
    ("grants",             "Grants",                 "🛡"),
    ("validation",         "Validation",             "✅"),
]


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m2 = divmod(m, 60)
    return f"{h}h {m2:02d}m {s:02d}s"


def _fmt_rows(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _svg_donut(success: int, failure: int, total: int) -> str:
    """Generate an SVG donut chart showing success vs failure ratio."""
    if total == 0:
        return ""
    cx, cy, r_outer, r_inner = 80, 80, 70, 45
    rate = success / total if total > 0 else 1.0
    1.0 - rate

    def arc_path(start_angle: float, end_angle: float, ro: float, ri: float) -> str:
        def pt(a: float, radius: float) -> tuple[float, float]:
            rad = math.radians(a - 90)
            return cx + radius * math.cos(rad), cy + radius * math.sin(rad)
        x1, y1 = pt(start_angle, ro)
        x2, y2 = pt(end_angle, ro)
        x3, y3 = pt(end_angle, ri)
        x4, y4 = pt(start_angle, ri)
        large = 1 if (end_angle - start_angle) > 180 else 0
        return (f"M {x1:.2f} {y1:.2f} "
                f"A {ro} {ro} 0 {large} 1 {x2:.2f} {y2:.2f} "
                f"L {x3:.2f} {y3:.2f} "
                f"A {ri} {ri} 0 {large} 0 {x4:.2f} {y4:.2f} Z")

    success_deg = rate * 360
    fail_deg = 360 - success_deg

    paths = ""
    if success > 0:
        paths += f'<path d="{arc_path(0, success_deg - 0.5 if fail_deg > 0.5 else success_deg, r_outer, r_inner)}" fill="#10b981" opacity="0.9"/>'
    if failure > 0:
        paths += f'<path d="{arc_path(success_deg + 0.5 if success > 0 else 0, 360, r_outer, r_inner)}" fill="#ef4444" opacity="0.9"/>'

    pct_label = f"{int(rate * 100)}%"
    return f"""<svg width="160" height="160" viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg">
  <circle cx="{cx}" cy="{cy}" r="{r_outer}" fill="#1f2937"/>
  {paths}
  <circle cx="{cx}" cy="{cy}" r="{r_inner}" fill="#0a0f1e"/>
  <text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle"
        font-size="18" font-weight="700" font-family="system-ui" fill="#f9fafb">{pct_label}</text>
  <text x="{cx}" y="{cy + 18}" text-anchor="middle" dominant-baseline="middle"
        font-size="10" font-family="system-ui" fill="#9ca3af">success</text>
</svg>"""


def _svg_bar_chart(table_stats: dict[str, dict], max_width: int = 420) -> str:
    """Generate a horizontal bar chart for rows per table."""
    if not table_stats:
        return ""
    max_rows = max((s.get("source", 0) for s in table_stats.values()), default=1) or 1
    bar_h = 22
    gap = 8
    label_w = 130
    padding = 20
    n = len(table_stats)
    height = padding * 2 + n * (bar_h + gap)
    bars = ""
    for i, (name, stats) in enumerate(table_stats.items()):
        src = stats.get("source", 0)
        suc = stats.get("success", 0)
        fail = stats.get("failure", 0)
        y = padding + i * (bar_h + gap)
        bar_max = max_width - label_w - 60
        success_w = int((suc / max_rows) * bar_max) if max_rows > 0 else 0
        fail_w = int((fail / max_rows) * bar_max) if max_rows > 0 else 0
        bars += f"""
  <text x="{label_w - 8}" y="{y + bar_h//2 + 4}" text-anchor="end" font-size="11"
        font-family="system-ui" fill="#9ca3af">{name[:18]}</text>
  <rect x="{label_w}" y="{y}" width="{success_w}" height="{bar_h}" rx="4" fill="#10b981" opacity="0.85"/>
  <rect x="{label_w + success_w}" y="{y}" width="{fail_w}" height="{bar_h}" rx="4" fill="#ef4444" opacity="0.85"/>
  <text x="{label_w + success_w + fail_w + 6}" y="{y + bar_h//2 + 4}" font-size="10"
        font-family="system-ui" fill="#6b7280">{_fmt_rows(src)}</text>"""

    return f"""<svg width="{max_width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <rect width="{max_width}" height="{height}" fill="transparent"/>
  {bars}
</svg>"""


class ReportBuilder:
    def __init__(self, result: dict[str, Any], start_time: float, end_time: float) -> None:
        self._result = result
        self._start_time = start_time
        self._end_time = end_time

    def _duration(self) -> float:
        return round(self._end_time - self._start_time, 2)

    def _ts(self, t: float) -> str:
        return datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------
    def build_json(self) -> dict[str, Any]:
        return {
            "report_version": "2.0",
            "run_id": self._result.get("run_id"),
            "mode": self._result.get("mode"),
            "started_at": self._ts(self._start_time),
            "completed_at": self._ts(self._end_time),
            "duration_seconds": self._duration(),
            "status": self._result.get("status"),
            "phases": self._result.get("phases", {}),
            "preflight": self._result.get("preflight"),
            "error": self._result.get("error") or self._result.get("cdc_error"),
        }

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------
    def build_html(self) -> str:
        data = self.build_json()
        status = data.get("status") or "unknown"
        phases = data.get("phases", {})
        mode = data.get("mode", "full")
        run_id = data.get("run_id", "—")
        duration = _fmt_duration(self._duration())

        # ---- Collect table-level stats ----
        objects: list[str] = phases.get("discover", {}).get("objects", [])
        table_stats: dict[str, dict] = {}
        total_src = total_suc = total_fail = 0
        for obj in objects:
            od = phases.get(obj, {})
            src = od.get("source_rows", od.get("initial_sync_rows", 0)) or 0
            suc = od.get("success", 0) or 0
            fail = od.get("failure", 0) or 0
            table_stats[obj] = {"source": src, "success": suc, "failure": fail}
            total_src += src
            total_suc += suc
            total_fail += fail

        success_rate = f"{int(total_suc / total_src * 100)}%" if total_src > 0 else "—"

        # ---- Status badge ----
        if status in ("success", "completed"):
            status_label, badge_class = "COMPLETED", "badge-success"
        elif status in ("failed", "cdc_failed"):
            status_label, badge_class = "FAILED", "badge-error"
        else:
            status_label, badge_class = status.upper(), "badge-warn"

        # ---- Phase timeline rows ----
        timeline_rows = ""
        for key, label, icon in _PHASE_META:
            phase_val = phases.get(key)
            if phase_val is None:
                row_status, row_class = "skipped", "phase-skip"
            elif isinstance(phase_val, str) and phase_val.startswith("error"):
                row_status, row_class = "error", "phase-error"
            else:
                row_status, row_class = "success", "phase-success"

            # Summarise phase detail
            detail = ""
            if isinstance(phase_val, dict):
                count = len(phase_val)
                detail = f"{count} item{'s' if count != 1 else ''}"
            elif isinstance(phase_val, list):
                detail = f"{len(phase_val)} item{'s' if len(phase_val) != 1 else ''}"
            elif isinstance(phase_val, str):
                detail = phase_val[:80]

            row_icon = "✓" if row_class == "phase-success" else ("✗" if row_class == "phase-error" else "—")
            timeline_rows += f"""
        <div class="phase-row {row_class}">
          <div class="phase-dot">{row_icon}</div>
          <div class="phase-info">
            <div class="phase-name">{icon} {label}</div>
            <div class="phase-detail">{detail or '—'}</div>
          </div>
          <div class="phase-badge">{row_status}</div>
        </div>"""

        # ---- Data table rows ----
        data_rows = ""
        for obj, stats in table_stats.items():
            src = stats["source"]
            suc = stats["success"]
            fail = stats["failure"]
            rate_pct = int(suc / src * 100) if src > 0 else 100
            rate_label = f"{rate_pct}%"
            bar_color = "#10b981" if fail == 0 else "#f59e0b"
            row_class = "" if fail == 0 else "row-warn"
            data_rows += f"""
        <tr class="{row_class}">
          <td class="td-table">{obj}</td>
          <td class="td-num">{_fmt_rows(src)}</td>
          <td class="td-num" style="color:#34d399">{_fmt_rows(suc)}</td>
          <td class="td-num" style="color:{'#f87171' if fail > 0 else '#4b5563'}">{fail:,}</td>
          <td><div class="mini-bar-bg"><div class="mini-bar-fill" style="width:{rate_pct}%;background:{bar_color}"></div></div></td>
          <td class="td-num">{rate_label}</td>
        </tr>"""

        if not data_rows:
            data_rows = '<tr><td colspan="6" style="text-align:center;color:#4b5563">No table data</td></tr>'

        # ---- Validation ----
        validation = phases.get("validation", {})
        val_html = ""
        if validation and isinstance(validation, dict):
            checks = validation.get("checks", {})
            v_mode = validation.get("mode", "")
            rows_val = ""
            for obj, check in checks.items():
                match = check.get("match", False)
                src_c = check.get("source_count", "—")
                tgt_c = check.get("target_count", "—")
                rows_val += f"""<tr>
          <td class="td-table">{obj}</td>
          <td class="td-num">{src_c}</td>
          <td class="td-num">{tgt_c}</td>
          <td><span class="badge-small {'badge-success' if match else 'badge-error'}">{'MATCH' if match else 'MISMATCH'}</span></td>
        </tr>"""
            val_html = f"""<section class="section">
        <h2 class="section-title">✅ Validation <span class="section-sub">({v_mode})</span></h2>
        <table class="data-table"><thead><tr><th>Table</th><th>Source</th><th>Target</th><th>Result</th></tr></thead>
        <tbody>{rows_val or '<tr><td colspan=4 style="text-align:center;color:#4b5563">No validation data</td></tr>'}</tbody></table>
      </section>"""

        # ---- PostgreSQL preflight / migration plan ----
        preflight = data.get("preflight")
        preflight_html = ""
        if isinstance(preflight, dict):
            schema_status_labels = {
                "COMPATIBLE": "Compatible",
                "TARGET_MISSING": "Target Missing",
                "MISMATCH": "Schema Mismatch",
            }
            action_labels = {
                "MIGRATE": "Update & Migrate",
                "CREATE_AND_MIGRATE": "Create & Migrate",
                "BLOCK": "Migration Blocked",
            }
            plan_rows = ""
            for object_plan in preflight.get("objects", []):
                details = object_plan.get("blockers") or object_plan.get("warnings") or ["—"]
                schema_status = (object_plan.get("schema_comparison") or {}).get("status")
                target_rows = object_plan.get("target_row_count")
                plan_rows += f"""<tr>
          <td class="td-table">{object_plan.get('object_name') or '—'}</td>
          <td>{schema_status_labels.get(schema_status, '—')}</td>
          <td class="td-num">{object_plan.get('source_row_count') if object_plan.get('source_row_count') is not None else '—'}</td>
          <td class="td-num">{target_rows if target_rows is not None else 0}</td>
          <td><span class="badge-small {'badge-success' if object_plan.get('readiness') == 'READY' else 'badge-error'}">{action_labels.get(object_plan.get('action'), 'Migration Blocked')}</span></td>
          <td>{'<br>'.join(str(detail) for detail in details)}</td>
        </tr>"""
            plan_blockers = preflight.get("blockers", [])
            preflight_html = f"""<section class="section">
        <h2 class="section-title">🧭 PostgreSQL Migration Plan <span class="section-sub">{'READY' if preflight.get('ready') else 'BLOCKED'}</span></h2>
        <p class="section-sub">Source connected: {preflight.get('source_connected', False)} · Target connected: {preflight.get('target_connected', False)} · Plan blockers: {len(plan_blockers)}</p>
        <div style="overflow-x:auto"><table class="data-table"><thead><tr><th>Table</th><th>Schema Status</th><th style="text-align:right">Source Rows</th><th style="text-align:right">Target Rows</th><th>Migration Action</th><th>Warnings / Blockers</th></tr></thead>
        <tbody>{plan_rows or '<tr><td colspan=6 style="text-align:center;color:#4b5563">No objects discovered</td></tr>'}</tbody></table></div>
      </section>"""

        # ---- Errors ----
        error_html = ""
        err = data.get("error")
        if err:
            error_html = f"""<section class="section error-section">
        <h2 class="section-title" style="color:#f87171">⚠ Errors</h2>
        <pre class="error-pre">{str(err)}</pre>
      </section>"""

        # ---- Charts ----
        donut = _svg_donut(total_suc, total_fail, total_src)
        bar_chart = _svg_bar_chart(table_stats)

        # ---- Phase accordion details ----
        accordion_items = ""
        for key, label, icon in _PHASE_META:
            val = phases.get(key)
            if val is None:
                continue
            raw = json.dumps(val, indent=2, default=str) if not isinstance(val, str) else val
            accordion_items += f"""
        <details class="accordion-item">
          <summary class="accordion-summary">{icon} {label}</summary>
          <pre class="accordion-body">{raw[:4000]}</pre>
        </details>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta name="description" content="Migration Platform Report — Run {run_id}"/>
  <title>Migration Report — {run_id[:12]}…</title>
  <style>
    :root {{
      --bg:#0a0f1e; --surface:#111827; --surface2:#1a2234;
      --border:#1f2937; --border2:#2d3748;
      --primary:#6366f1; --primary-glow:rgba(99,102,241,0.15);
      --success:#10b981; --error:#ef4444; --warn:#f59e0b;
      --text:#f9fafb; --muted:#9ca3af; --muted2:#6b7280;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ scroll-behavior: smooth; }}
    body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; line-height: 1.6; }}

    /* Hero */
    .hero {{ background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 50%, #0a0f1e 100%); padding: 3rem 2rem 2.5rem; text-align: center; border-bottom: 1px solid var(--border); position: relative; overflow: hidden; }}
    .hero::before {{ content: ''; position: absolute; inset: 0; background: radial-gradient(ellipse at 50% 0%, rgba(99,102,241,0.15), transparent 60%); pointer-events: none; }}
    .hero-logo {{ font-size: 2.5rem; font-weight: 800; background: linear-gradient(90deg, #818cf8, #c084fc, #60a5fa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; margin-bottom: 0.5rem; }}
    .hero-subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .hero-badges {{ display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap; margin-bottom: 1.5rem; }}
    .run-id-label {{ font-family: 'Menlo', 'Monaco', 'Courier New', monospace; font-size: 0.78rem; color: var(--muted2); background: var(--surface2); border: 1px solid var(--border); border-radius: 0.4rem; padding: 0.25rem 0.6rem; }}

    /* Badges */
    .badge-success {{ background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.35); padding: 0.3rem 1rem; border-radius: 9999px; font-weight: 700; font-size: 0.85rem; display: inline-block; }}
    .badge-error   {{ background: rgba(239,68,68,0.15);  color: #f87171; border: 1px solid rgba(239,68,68,0.35);  padding: 0.3rem 1rem; border-radius: 9999px; font-weight: 700; font-size: 0.85rem; display: inline-block; }}
    .badge-warn    {{ background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.35); padding: 0.3rem 1rem; border-radius: 9999px; font-weight: 700; font-size: 0.85rem; display: inline-block; }}
    .badge-small   {{ padding: 0.15rem 0.5rem; border-radius: 0.3rem; font-size: 0.75rem; font-weight: 600; }}
    .badge-mode    {{ background: var(--primary-glow); color: #818cf8; border: 1px solid rgba(99,102,241,0.35); padding: 0.3rem 1rem; border-radius: 9999px; font-weight: 600; font-size: 0.82rem; display: inline-block; }}

    /* Layout */
    .container {{ max-width: 1100px; margin: 0 auto; padding: 2.5rem 1.5rem; }}

    /* Summary cards */
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2.5rem; }}
    .summary-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 1rem; padding: 1.5rem; transition: border-color 0.2s, transform 0.2s; position: relative; overflow: hidden; }}
    .summary-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; border-radius: 1rem 1rem 0 0; }}
    .summary-card.green::before {{ background: linear-gradient(90deg, var(--success), #059669); }}
    .summary-card.blue::before  {{ background: linear-gradient(90deg, var(--primary), #4f46e5); }}
    .summary-card.purple::before {{ background: linear-gradient(90deg, #c084fc, #9333ea); }}
    .summary-card.amber::before {{ background: linear-gradient(90deg, var(--warn), #d97706); }}
    .summary-card:hover {{ border-color: var(--border2); transform: translateY(-2px); }}
    .card-icon {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
    .card-label {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 0.35rem; }}
    .card-value {{ font-size: 2rem; font-weight: 700; }}

    /* Sections */
    .section {{ margin-bottom: 2.5rem; }}
    .section-title {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 1.25rem; color: var(--text); }}
    .section-sub {{ font-size: 0.8rem; font-weight: 400; color: var(--muted); margin-left: 0.5rem; }}

    /* Phase timeline */
    .phase-timeline {{ display: flex; flex-direction: column; gap: 0; }}
    .phase-row {{ display: grid; grid-template-columns: 2rem 1fr auto; align-items: center; gap: 0.75rem; padding: 0.6rem 1rem; border-left: 3px solid var(--border); background: var(--surface); margin-bottom: 2px; border-radius: 0 0.5rem 0.5rem 0; transition: background 0.15s; }}
    .phase-row:hover {{ background: var(--surface2); }}
    .phase-success {{ border-left-color: var(--success); }}
    .phase-error   {{ border-left-color: var(--error); }}
    .phase-skip    {{ border-left-color: var(--border); opacity: 0.5; }}
    .phase-dot {{ font-size: 0.9rem; text-align: center; }}
    .phase-success .phase-dot {{ color: var(--success); }}
    .phase-error   .phase-dot {{ color: var(--error); }}
    .phase-skip    .phase-dot {{ color: var(--muted2); }}
    .phase-name {{ font-size: 0.9rem; font-weight: 600; }}
    .phase-detail {{ font-size: 0.78rem; color: var(--muted); margin-top: 0.1rem; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 400px; }}
    .phase-badge {{ font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding: 0.15rem 0.5rem; border-radius: 0.3rem; }}
    .phase-success .phase-badge {{ background: rgba(16,185,129,0.15); color: #34d399; }}
    .phase-error .phase-badge {{ background: rgba(239,68,68,0.15); color: #f87171; }}
    .phase-skip .phase-badge {{ background: var(--surface2); color: var(--muted2); }}

    /* Data table */
    .data-table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    .data-table thead {{ background: var(--surface2); }}
    .data-table th {{ padding: 0.65rem 1rem; text-align: left; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); border-bottom: 1px solid var(--border2); }}
    .data-table td {{ padding: 0.65rem 1rem; border-bottom: 1px solid var(--border); }}
    .data-table tbody tr:hover {{ background: var(--surface2); }}
    .td-table {{ font-weight: 600; font-family: 'Menlo','Monaco','Courier New',monospace; font-size: 0.82rem; }}
    .td-num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .mini-bar-bg {{ height: 8px; background: var(--border2); border-radius: 9999px; overflow: hidden; min-width: 80px; }}
    .mini-bar-fill {{ height: 100%; border-radius: 9999px; transition: width 0.3s; }}
    .row-warn td {{ background: rgba(245,158,11,0.03); }}

    /* Charts */
    .charts-grid {{ display: grid; grid-template-columns: auto 1fr; gap: 2rem; align-items: center; padding: 1.5rem; background: var(--surface); border: 1px solid var(--border); border-radius: 1rem; }}
    @media (max-width: 600px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
    .chart-label {{ font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 0.75rem; }}
    .donut-legend {{ display: flex; flex-direction: column; gap: 0.4rem; margin-top: 0.75rem; }}
    .legend-item {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; }}
    .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}

    /* Accordion */
    .accordion {{ background: var(--surface); border: 1px solid var(--border); border-radius: 1rem; overflow: hidden; }}
    .accordion-item {{ border-bottom: 1px solid var(--border); }}
    .accordion-item:last-child {{ border-bottom: none; }}
    .accordion-summary {{ list-style: none; padding: 0.85rem 1.25rem; font-size: 0.88rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.5rem; transition: background 0.15s; user-select: none; }}
    .accordion-summary:hover {{ background: var(--surface2); }}
    .accordion-summary::marker {{ display: none; }}
    details[open] .accordion-summary {{ color: var(--primary); background: var(--surface2); }}
    .accordion-body {{ padding: 1rem 1.25rem; font-family: 'Menlo','Monaco','Courier New',monospace; font-size: 0.78rem; line-height: 1.7; color: var(--muted); white-space: pre-wrap; word-break: break-word; background: var(--bg); border-top: 1px solid var(--border); max-height: 300px; overflow-y: auto; }}

    /* Error section */
    .error-section {{ background: rgba(239,68,68,0.05); border: 1px solid rgba(239,68,68,0.25); border-radius: 1rem; padding: 1.5rem; }}
    .error-pre {{ font-family: monospace; font-size: 0.82rem; color: #fca5a5; white-space: pre-wrap; word-break: break-word; }}

    /* Footer */
    footer {{ text-align: center; padding: 2.5rem 1.5rem; color: var(--muted2); font-size: 0.78rem; border-top: 1px solid var(--border); margin-top: 1rem; }}
    footer a {{ color: var(--muted); text-decoration: none; }}

    /* Print */
    @media print {{
      body {{ background: white; color: #111; }}
      .hero {{ background: #f8fafc; border-bottom: 2px solid #e2e8f0; }}
      .hero-logo {{ -webkit-text-fill-color: #4338ca; color: #4338ca; }}
      .summary-card, .section {{ break-inside: avoid; }}
    }}
  </style>
</head>
<body>

<!-- Hero -->
<header class="hero">
  <div class="hero-logo">🚀 Migration Report</div>
  <div class="hero-subtitle">Migration Platform — End-to-End Data Migration</div>
  <div class="hero-badges">
    <span class="{badge_class}">{status_label}</span>
    <span class="badge-mode">{mode.upper()}</span>
  </div>
  <div class="run-id-label">Run ID: {run_id}</div>
</header>

<div class="container">

  <!-- Summary Cards -->
  <div class="summary-grid">
    <div class="summary-card green">
      <div class="card-icon">📋</div>
      <div class="card-label">Tables Migrated</div>
      <div class="card-value">{len(objects)}</div>
    </div>
    <div class="summary-card blue">
      <div class="card-icon">📦</div>
      <div class="card-label">Total Rows</div>
      <div class="card-value">{_fmt_rows(total_src)}</div>
    </div>
    <div class="summary-card purple">
      <div class="card-icon">✅</div>
      <div class="card-label">Success Rate</div>
      <div class="card-value">{success_rate}</div>
    </div>
    <div class="summary-card amber">
      <div class="card-icon">⏱</div>
      <div class="card-label">Duration</div>
      <div class="card-value" style="font-size:1.5rem">{duration}</div>
    </div>
  </div>

  <!-- Charts -->
  {'<section class="section"><h2 class="section-title">📊 Migration Overview</h2><div class="charts-grid"><div><div class="chart-label">Success Ratio</div>' + donut + '<div class="donut-legend"><div class="legend-item"><div class="legend-dot" style="background:#10b981"></div><span>Migrated: ' + _fmt_rows(total_suc) + '</span></div><div class="legend-item"><div class="legend-dot" style="background:#ef4444"></div><span>Failed: ' + str(total_fail) + '</span></div></div></div><div><div class="chart-label">Rows Per Table</div>' + bar_chart + '</div></div></section>' if table_stats else ''}

  <!-- Phase Timeline -->
  <section class="section">
    <h2 class="section-title">🗓 Migration Timeline <span class="section-sub">({len(_PHASE_META)} phases)</span></h2>
    <div class="phase-timeline">{timeline_rows}
    </div>
  </section>

  <!-- Data Migration Table -->
  {'<section class="section"><h2 class="section-title">📦 Data Migration <span class="section-sub">(' + str(len(objects)) + ' tables)</span></h2><div style="overflow-x:auto"><table class="data-table"><thead><tr><th>Table</th><th style="text-align:right">Source Rows</th><th style="text-align:right">Migrated</th><th style="text-align:right">Failed</th><th>Rate</th><th style="text-align:right">%</th></tr></thead><tbody>' + data_rows + '</tbody></table></div></section>' if table_stats else ''}

  <!-- Validation -->
  {val_html}

  <!-- PostgreSQL Preflight / Plan -->
  {preflight_html}

  <!-- Phase Details (accordion) -->
  <section class="section">
    <h2 class="section-title">📋 Phase Details <span class="section-sub">(click to expand)</span></h2>
    <div class="accordion">{accordion_items or '<div style="padding:1.5rem;color:var(--muted);text-align:center">No phase details available</div>'}</div>
  </section>

  <!-- Errors -->
  {error_html}

</div>

<!-- Footer -->
<footer>
  <div>Generated by <strong>Migration Platform</strong> &nbsp;•&nbsp; {self._ts(self._end_time)} &nbsp;•&nbsp; Report v2.0</div>
  <div style="margin-top:0.5rem">Started: {data.get('started_at')} &nbsp;→&nbsp; Completed: {data.get('completed_at')}</div>
</footer>

</body>
</html>"""

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    def save(self, directory: str = "reports") -> tuple[str, str]:
        os.makedirs(directory, exist_ok=True)
        run_id = self._result.get("run_id", "unknown")
        html_path = os.path.join(directory, f"{run_id}.html")
        json_path = os.path.join(directory, f"{run_id}.json")

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(self.build_html())

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.build_json(), f, indent=2, default=str)

        return html_path, json_path
