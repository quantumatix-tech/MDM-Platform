from __future__ import annotations

import errno
import json
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from core.audit_logger import audit_log, get_run_id

# ---------------------------------------------------------------------------
# Live dashboard HTML (served at GET /)
# ---------------------------------------------------------------------------
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Migration Platform — Live Dashboard</title>
  <style>
    :root {
      --bg: #0a0f1e; --surface: #111827; --border: #1f2937;
      --primary: #6366f1; --success: #10b981; --error: #ef4444;
      --warn: #f59e0b; --text: #f9fafb; --muted: #9ca3af;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height: 100vh; }
    header { background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%); padding: 1.5rem 2rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 1rem; }
    header .logo { font-size: 1.5rem; font-weight: 700; background: linear-gradient(90deg, #818cf8, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    header .subtitle { color: var(--muted); font-size: 0.85rem; }
    .badge { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.25rem 0.75rem; border-radius: 9999px; font-size: 0.78rem; font-weight: 600; }
    .badge.running { background: rgba(99,102,241,0.2); color: #818cf8; border: 1px solid rgba(99,102,241,0.4); }
    .badge.success { background: rgba(16,185,129,0.2); color: #34d399; border: 1px solid rgba(16,185,129,0.4); }
    .badge.failed { background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid rgba(239,68,68,0.4); }
    .badge.idle { background: rgba(156,163,175,0.2); color: #9ca3af; border: 1px solid rgba(156,163,175,0.3); }
    .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
    .meta-bar { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .meta-card { background: var(--surface); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1rem 1.5rem; flex: 1; min-width: 160px; }
    .meta-card .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 0.3rem; }
    .meta-card .value { font-size: 1.1rem; font-weight: 600; }
    .progress-section { background: var(--surface); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1.5rem; margin-bottom: 2rem; }
    .progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
    .progress-title { font-weight: 600; font-size: 1rem; }
    .progress-pct { font-size: 1.25rem; font-weight: 700; color: var(--primary); }
    .progress-bar-bg { height: 12px; background: var(--border); border-radius: 9999px; overflow: hidden; }
    .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--primary), #a78bfa); border-radius: 9999px; transition: width 0.5s ease; }
    .current-phase { margin-top: 0.75rem; color: var(--muted); font-size: 0.85rem; }
    .current-phase span { color: var(--text); font-weight: 500; }
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2rem; }
    @media (max-width: 700px) { .two-col { grid-template-columns: 1fr; } }
    .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1.5rem; }
    .panel h3 { font-size: 0.9rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 1rem; }
    .phase-list { list-style: none; }
    .phase-list li { display: flex; align-items: center; gap: 0.5rem; padding: 0.3rem 0; font-size: 0.88rem; border-bottom: 1px solid #1f2937; }
    .phase-list li:last-child { border-bottom: none; }
    .phase-icon { font-size: 0.9rem; width: 1.2rem; text-align: center; }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
    th { text-align: left; padding: 0.5rem 0.75rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); border-bottom: 1px solid var(--border); }
    td { padding: 0.55rem 0.75rem; border-bottom: 1px solid #1a2234; }
    .mini-bar-bg { height: 6px; background: var(--border); border-radius: 9999px; overflow: hidden; min-width: 60px; }
    .mini-bar-fill { height: 100%; background: var(--success); border-radius: 9999px; }
    .errors-panel { background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.3); border-radius: 0.75rem; padding: 1.5rem; margin-top: 1.5rem; display: none; }
    .errors-panel.has-errors { display: block; }
    .errors-panel h3 { color: #f87171; margin-bottom: 0.75rem; font-size: 0.9rem; }
    .errors-panel ul { list-style: none; }
    .errors-panel li { font-size: 0.82rem; padding: 0.3rem 0; color: #fca5a5; border-bottom: 1px solid rgba(239,68,68,0.15); }
    .refresh-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--success); margin-right: 0.4rem; animation: pulse 2s infinite; }
    .object-status-migrated { color: #34d399; font-weight: 700; }
    .object-status-blocked, .object-status-failed { color: #f87171; font-weight: 700; }
    .object-status-unsupported { color: #fbbf24; font-weight: 700; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
    footer { text-align: center; color: var(--muted); font-size: 0.78rem; padding: 2rem; margin-top: 1rem; }
  </style>
</head>
<body>
<header>
  <div>
    <div class="logo">🚀 Migration Platform</div>
    <div class="subtitle">Live Migration Dashboard</div>
  </div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:0.75rem">
    <span style="font-size:0.78rem;color:var(--muted)"><span class="refresh-dot"></span>Auto-refresh 2s</span>
    <span id="status-badge" class="badge idle">Idle</span>
  </div>
</header>

<div class="container">
  <div class="meta-bar">
    <div class="meta-card"><div class="label">Run ID</div><div class="value" id="run-id" style="font-size:0.85rem;font-family:monospace">—</div></div>
    <div class="meta-card"><div class="label">Mode</div><div class="value" id="mode">—</div></div>
    <div class="meta-card"><div class="label">Started</div><div class="value" id="started-at" style="font-size:0.9rem">—</div></div>
    <div class="meta-card"><div class="label">Elapsed</div><div class="value" id="elapsed">—</div></div>
    <div class="meta-card"><div class="label">Errors</div><div class="value" id="error-count" style="color:var(--success)">0</div></div>
  </div>

  <div class="progress-section">
    <div class="progress-header">
      <span class="progress-title">Overall Progress</span>
      <span class="progress-pct" id="pct">0%</span>
    </div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="progress-fill" style="width:0%"></div>
    </div>
    <div class="current-phase">Current phase: <span id="current-phase">Waiting…</span></div>
  </div>

  <div class="two-col">
    <div class="panel">
      <h3>Phase Status</h3>
      <ul class="phase-list" id="phase-list">
        <li><span class="phase-icon">○</span> Waiting for migration to start…</li>
      </ul>
    </div>
    <div class="panel">
      <h3>Table Statistics</h3>
      <table>
        <thead><tr><th>Table</th><th>Source</th><th>Migrated</th><th>Failed</th><th>Rate</th></tr></thead>
        <tbody id="table-stats"></tbody>
      </table>
    </div>
  </div>

  <div class="panel" style="margin-top:1.5rem">
    <h3>Object Migration Results</h3>
    <table>
      <thead><tr><th>Category</th><th>Source</th><th>Migrated</th><th>Blocked</th><th>Unsupported</th><th>Failed</th><th>Status</th></tr></thead>
      <tbody id="object-migration"></tbody>
    </table>
  </div>

  <div id="errors-panel" class="errors-panel">
    <h3>⚠ Errors</h3>
    <ul id="error-list"></ul>
  </div>
</div>

<footer>Migration Platform • Live Dashboard • <span id="last-updated">—</span></footer>

<script>
  const PHASES = [
    'connect','ensure_database','extensions','schemas','custom_types',
    'create_sequences','create_tables','create_partitions',
    'data','apply_constraints','row_level_security','advance_sequences',
    'views','materialized_views','functions','triggers',
      'comments','security_principals','grants','validation'
  ];

  function fmt(n) { return n >= 1000 ? n.toLocaleString() : String(n); }
  function phaseName(k) {
    return { connect:'Connect', ensure_database:'Ensure Database',
      extensions:'Extensions', schemas:'Schemas', custom_types:'Custom Types',
      create_sequences:'Create Sequences', create_tables:'Create Tables',
      create_partitions:'Create Partitions', data:'Migrate Data',
      apply_constraints:'Indexes + Constraints', row_level_security:'Row-Level Security',
      advance_sequences:'Advance Sequences', views:'Views',
      materialized_views:'Materialized Views', functions:'Functions & Procedures',
      triggers:'Triggers', comments:'Comments', security_principals:'Security & Access', grants:'Grants',
      partition_testing:'Partition Testing', validation:'Validation'
    }[k] || k;
  }

  async function refresh() {
    try {
      const r = await fetch('/status');
      const d = await r.json();
      const pct = d.progress || 0;

      document.getElementById('run-id').textContent = d.run_id || '—';
      document.getElementById('mode').textContent = (d.mode || '—').toUpperCase();
      document.getElementById('pct').textContent = pct + '%';
      document.getElementById('progress-fill').style.width = pct + '%';
      document.getElementById('current-phase').textContent = d.phase || 'Waiting…';
      document.getElementById('error-count').textContent = (d.errors || []).length;
      document.getElementById('error-count').style.color = (d.errors||[]).length > 0 ? 'var(--error)' : 'var(--success)';
      document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();

      if (d.started_at) document.getElementById('started-at').textContent = new Date(d.started_at * 1000).toLocaleTimeString();
      if (d.elapsed_s !== undefined) {
        const e = Math.round(d.elapsed_s);
        document.getElementById('elapsed').textContent = `${Math.floor(e/60).toString().padStart(2,'0')}:${(e%60).toString().padStart(2,'0')}`;
      }

      const badge = document.getElementById('status-badge');
      if (pct === 100) { badge.textContent = '✓ Completed'; badge.className = 'badge success'; }
      else if ((d.errors||[]).length > 0) { badge.textContent = '⚠ Errors'; badge.className = 'badge failed'; }
      else if (pct > 0) { badge.textContent = '⟳ Running'; badge.className = 'badge running'; }
      else { badge.textContent = 'Idle'; badge.className = 'badge idle'; }

      // Phase list
      const done = d.phases_done || [];
      const current = d.phase || '';
      const ul = document.getElementById('phase-list');
      ul.innerHTML = PHASES.map(k => {
        const isDone = done.includes(k);
        const isActive = current.startsWith(k) || current === k;
        const icon = isDone ? '✓' : isActive ? '⟳' : '○';
        const color = isDone ? 'var(--success)' : isActive ? 'var(--primary)' : 'var(--muted)';
        return `<li><span class="phase-icon" style="color:${color}">${icon}</span><span style="color:${isDone?'var(--text)':isActive?'#a5b4fc':'var(--muted)'}">${phaseName(k)}</span></li>`;
      }).join('');

      // Table stats
      const tbody = document.getElementById('table-stats');
      const tables = d.table_stats || {};
      if (Object.keys(tables).length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted);text-align:center">No data yet</td></tr>';
      } else {
        tbody.innerHTML = Object.entries(tables).map(([name, s]) => {
          const pct2 = s.source > 0 ? Math.round(s.success / s.source * 100) : 100;
          const col = s.failure > 0 ? 'var(--error)' : 'var(--success)';
          return `<tr>
            <td style="font-weight:500">${name}</td>
            <td>${fmt(s.source||0)}</td>
            <td>${fmt(s.success||0)}</td>
            <td style="color:${s.failure>0?'var(--error)':'var(--muted)'}">${s.failure||0}</td>
            <td><div class="mini-bar-bg"><div class="mini-bar-fill" style="width:${pct2}%;background:${col}"></div></div></td>
          </tr>`;
        }).join('');
      }

      const objectBody = document.getElementById('object-migration');
      const categories = (d.object_migration || {}).categories || {};
      const categoryEntries = Object.entries(categories);
      objectBody.innerHTML = categoryEntries.length === 0
        ? '<tr><td colspan="7" style="color:var(--muted);text-align:center">No object results yet</td></tr>'
        : categoryEntries.map(([name, row]) => { const status = (row.status || 'UNKNOWN').toLowerCase(); const label = name === 'functions' ? 'FUNCTION' : name === 'triggers' ? 'TRIGGER' : ''; const detail = row.status === 'BLOCKED' && label ? `${label}: BLOCKED<br>Please ask a MySQL administrator to run once:<br><code>SET PERSIST log_bin_trust_function_creators = ON;</code><br>This server configuration persists across MySQL restarts.<br>Then re-run the migration.` : (row.details || []).join('<br>'); return `<tr><td>${name.replaceAll('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase())}</td><td>${fmt(row.source_count || 0)}</td><td>${fmt(row.migrated || 0)}</td><td>${fmt(row.blocked || 0)}</td><td>${fmt(row.unsupported || 0)}</td><td>${fmt(row.failed || 0)}</td><td><span class="object-status-${status}">${row.status || 'UNKNOWN'}</span><br><small>${detail}</small></td></tr>`; }).join('');

      // Errors
      const errs = d.errors || [];
      const ep = document.getElementById('errors-panel');
      if (errs.length > 0) {
        ep.classList.add('has-errors');
        document.getElementById('error-list').innerHTML = errs.slice(-10).map(e => `<li>${e}</li>`).join('');
      } else {
        ep.classList.remove('has-errors');
      }
    } catch (e) {}
  }

  refresh();
  setInterval(refresh, 2000);
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            body = _DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/status":
            srv = self.server
            elapsed = time.time() - getattr(srv, "_start_time", time.time())
            status: dict[str, Any] = {
                "run_id": get_run_id(),
                "phase": getattr(srv, "_current_phase", "idle"),
                "progress": getattr(srv, "_progress", 0),
                "errors": getattr(srv, "_errors", []),
                "mode": getattr(srv, "_mode", ""),
                "started_at": getattr(srv, "_start_time", None),
                "elapsed_s": round(elapsed, 1),
                "table_stats": getattr(srv, "_table_stats", {}),
                "object_migration": getattr(srv, "_object_migration", {}),
                "phases_done": getattr(srv, "_phases_done", []),
            }
            body = json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/reports", "/reports/"):
            # --- Report index page ---
            reports_dir = getattr(self.server, "_reports_dir", "reports")
            body = self._build_reports_index(reports_dir).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/reports/"):
            # --- Serve a specific report file ---
            filename = self.path[len("/reports/"):]
            # Sanitize: only allow safe filenames (no path traversal)
            if ".." in filename or "/" in filename or "\\" in filename:
                self.send_response(403)
                self.end_headers()
                return
            reports_dir = getattr(self.server, "_reports_dir", "reports")
            filepath = os.path.join(reports_dir, filename)
            if not os.path.isfile(filepath):
                self.send_response(404)
                self.end_headers()
                return
            if filename.endswith(".html"):
                ct = "text/html; charset=utf-8"
            elif filename.endswith(".json"):
                ct = "application/json"
            else:
                ct = "application/octet-stream"
            with open(filepath, "rb") as f:
                body_bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        else:
            self.send_response(404)
            self.end_headers()

    def _build_reports_index(self, reports_dir: str) -> str:
        import glob
        html_files = sorted(
            glob.glob(os.path.join(reports_dir, "*.html")),
            key=os.path.getmtime,
            reverse=True,
        )
        rows = ""
        for path in html_files:
            name = os.path.basename(path)
            run_id = name.replace(".html", "")
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
            size_kb = os.path.getsize(path) // 1024
            rows += f"""<tr>
              <td style="font-family:monospace;font-size:0.82rem">{run_id[:16]}...</td>
              <td>{mtime.strftime('%Y-%m-%d %H:%M UTC')}</td>
              <td>{size_kb} KB</td>
              <td>
                <a href="/reports/{name}" style="color:#818cf8;text-decoration:none;margin-right:1rem">View HTML</a>
                <a href="/reports/{run_id}.json" style="color:#9ca3af;text-decoration:none">JSON</a>
              </td>
            </tr>"""
        if not rows:
            rows = '<tr><td colspan="4" style="text-align:center;color:#4b5563;padding:2rem">No reports yet</td></tr>'
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Migration Reports Archive</title>
<style>
  body{{background:#0a0f1e;color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:2rem}}
  h1{{background:linear-gradient(90deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;font-size:1.6rem;margin-bottom:0.5rem}}
  p{{color:#9ca3af;margin-bottom:2rem;font-size:0.88rem}}
  table{{width:100%;border-collapse:collapse;background:#111827;border-radius:0.75rem;overflow:hidden}}
  th{{padding:0.75rem 1rem;text-align:left;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em;color:#6b7280;background:#1a2234;border-bottom:1px solid #1f2937}}
  td{{padding:0.75rem 1rem;border-bottom:1px solid #1f2937;font-size:0.88rem}}
  tr:hover td{{background:#1a2234}}
  .back{{color:#818cf8;text-decoration:none;font-size:0.82rem;display:inline-block;margin-bottom:1rem}}
</style></head>
<body>
  <a href="/" class="back">&larr; Live Dashboard</a>
  <h1>Migration Reports Archive</h1>
  <p>All past migration reports &mdash; {len(html_files)} report{'s' if len(html_files)!=1 else ''} stored</p>
  <table>
    <thead><tr><th>Run ID</th><th>Generated</th><th>Size</th><th>Actions</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body></html>"""

    def log_message(self, format: str, *args: Any) -> None:  # suppress access logs
        pass


class _StatusHTTPServer(HTTPServer):
    allow_reuse_address = False


# ---------------------------------------------------------------------------
# StatusServer
# ---------------------------------------------------------------------------

class StatusServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._server: HTTPServer | None = None
        self._serving = False

    # ------------------------------------------------------------------ init
    def start(self) -> None:
        try:
            self._server = _StatusHTTPServer((self._host, self._port), StatusHandler)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            unavailable = exc.errno in {errno.EACCES, errno.EADDRINUSE} or winerror in {10013, 10048}
            if unavailable:
                raise RuntimeError(
                    f"Status server port {self._port} is unavailable on {self._host}. "
                    "Stop the process using that port and retry the migration."
                ) from exc
            raise
        else:
            audit_log(
                phase="status_server",
                status="started",
                details={"requested_port": self._port, "bound_port": self.port, "host": self.bound_host},
            )
        self._server._current_phase = "idle"
        self._server._progress = 0
        self._server._errors: list[str] = []
        self._server._table_stats: dict[str, dict] = {}
        self._server._object_migration: dict[str, Any] = {}
        self._server._phases_done: list[str] = []
        self._server._mode = ""
        self._server._start_time = time.time()
        self._server._reports_dir = "reports"   # served at /reports/

    def stop(self) -> None:
        if self._server is None:
            return
        if self._serving: self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._serving = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def bound_host(self) -> str:
        if self._server is None:
            return self._host
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    def set_mode(self, mode: str) -> None:
        if self._server is not None:
            self._server._mode = mode

    # ------------------------------------------------------------ status updates
    def update_status(self, phase: str, progress: int, errors: list[str] | None = None) -> None:
        if self._server is None:
            return
        prev = getattr(self._server, "_current_phase", "")
        # Mark previous clean phase as done (strip "data: ..." to just "data")
        if prev and prev != "idle" and prev != phase:
            base = prev.split(":")[0].strip().replace(" ", "_").lower()
            if base not in self._server._phases_done:
                self._server._phases_done.append(base)
        self._server._current_phase = phase
        self._server._progress = progress
        if errors:
            self._server._errors = errors

    def record_table_stats(self, table: str, source_rows: int, success: int, failure: int) -> None:
        """Update per-table migration statistics (called after each batch/table)."""
        if self._server is None:
            return
        self._server._table_stats[table] = {
            "source": source_rows,
            "success": success,
            "failure": failure,
        }

    def record_object_migration(self, result: dict[str, Any]) -> None:
        if self._server is not None:
            self._server._object_migration = result

    # -------------------------------------------------------------- blocking run
    def run(self) -> None:
        if self._server is None:
            self.start()
        self._serving = True
        try: self._server.serve_forever()
        except BaseException: self._serving = False; raise
        self._serving = False
