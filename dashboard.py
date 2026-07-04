"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(t.timestamp, 1, 10)        as day,
            COALESCE(t.model, 'unknown')      as model,
            COALESCE(s.source, 'claude-code') as source,
            SUM(t.input_tokens)               as input,
            SUM(t.output_tokens)              as output,
            SUM(t.cache_read_tokens)          as cache_read,
            SUM(t.cache_creation_tokens)      as cache_creation,
            COUNT(*)                          as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        GROUP BY day, t.model, s.source
        ORDER BY day, t.model
    """).fetchall()

    SOURCE_TO_PROVIDER = {
        "opencode":   "opencode",
        "openrouter": "OpenRouter",
        "claude-code": "Claude Code",
    }

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "provider":       SOURCE_TO_PROVIDER.get(r["source"], "Claude Code"),
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            source, actual_cost_usd
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        provider = SOURCE_TO_PROVIDER.get(r["source"], "Claude Code")
        # OpenRouter "sessions" are daily rollups — date is more useful than
        # the synthetic id prefix (which is identical for every OR row).
        if r["source"] == "openrouter":
            short_id = (r["last_timestamp"] or "")[:10] or r["session_id"][:8]
        else:
            short_id = r["session_id"][:8]
        sessions_all.append({
            "session_id":    short_id,
            "provider":      provider,
            "actual_cost":   r["actual_cost_usd"],
            "project":       r["project_name"] or "unknown",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    all_providers = sorted(
        {s["provider"] for s in sessions_all} | {d["provider"] for d in daily_by_model}
    )

    return {
        "all_models":     all_models,
        "all_providers":  all_providers,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes, #provider-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .provider-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; }
  .provider-tag.cc { background: rgba(217,119,87,0.15); color: var(--accent); }
  .provider-tag.oc { background: rgba(74,222,128,0.15); color: var(--green); }
  .provider-tag.or { background: rgba(167,139,250,0.15); color: #a78bfa; }
  .pager { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding-top: 12px; }
  .pager .page-info { color: var(--muted); font-size: 12px; margin-right: 4px; }
  .pager button { padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; }
  .pager button:hover:not(:disabled) { border-color: var(--accent); color: var(--text); }
  .pager button:disabled { opacity: 0.4; cursor: not-allowed; }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>AI Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
</header>

<div id="filter-bar">
  <div class="filter-label">Providers</div>
  <div id="provider-checkboxes"></div>
  <div class="filter-sep"></div>
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="3hr"  onclick="setRange('3hr')">3h</button>
    <button class="range-btn" data-range="6hr"  onclick="setRange('6hr')">6h</button>
    <button class="range-btn" data-range="12hr" onclick="setRange('12hr')">12h</button>
    <button class="range-btn" data-range="1d"   onclick="setRange('1d')">1d</button>
    <button class="range-btn" data-range="3d"   onclick="setRange('3d')">3d</button>
    <button class="range-btn" data-range="7d"   onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d"  onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d"  onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all"  onclick="setRange('all')">All</button>
  </div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Recent Sessions</div>
    <table>
      <thead><tr>
        <th>Session</th><th>Provider</th><th>Project</th><th>Last Active</th><th>Duration</th>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div id="sessions-pager" class="pager"></div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Claude cost estimates use Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of June 2026; actual costs for Max/Pro subscribers differ. opencode sessions are priced against the upstream provider's published rates (OpenAI, Google, Moonshot) and are approximate &mdash; see <code>dashboard.py</code> to override.</p>
    <p>
      GitHub: <a href="https://github.com/kierandrewett/ai-usage" target="_blank">github.com/kierandrewett/ai-usage</a>
      &nbsp;&middot;&nbsp;
      Forked from: <a href="https://github.com/phuryn/claude-usage" target="_blank">phuryn/claude-usage</a> by <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedProviders = new Set();
let selectedRange = '30d';
let charts = {};
let sessionsPage = 0;
const SESSIONS_PAGE_SIZE = 20;
let lastFilteredSessions = [];

// ── Pricing (per 1M tokens, USD) ───────────────────────────────────────────
// All entries are list API pricing for the upstream provider as of June 2026.
// Subscriber plans (Claude Max/Pro, ChatGPT Plus, etc.) bundle usage at a
// flat rate, so these figures are the API-equivalent cost of each session,
// not what a subscriber actually paid out-of-pocket.
const PRICING = {
  // Anthropic — list API pricing (cache_write = 1.25× input for 5m TTL, cache_read = 0.10× input)
  'claude-fable-5':          { input: 10.00, output: 50.00, cache_write: 12.50, cache_read: 1.00 },
  'claude-opus-4-8':         { input: 5.00, output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-opus-4-7':         { input: 5.00, output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-opus-4-6':         { input: 5.00, output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-opus-4-5':         { input: 5.00, output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-sonnet-4-6':       { input: 3.00, output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-sonnet-4-5':       { input: 3.00, output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-haiku-4-5':        { input: 1.00, output:  5.00, cache_write: 1.25, cache_read: 0.10 },
  // OpenAI (opencode) — cache_write billed at standard input rate
  'gpt-5.5':                 { input: 5.00, output: 30.00, cache_write: 5.00, cache_read: 0.50 },
  'gpt-5.4':                 { input: 2.50, output: 15.00, cache_write: 2.50, cache_read: 0.25 },
  'gpt-5.4-mini':            { input: 0.75, output:  4.50, cache_write: 0.75, cache_read: 0.075 },
  'gpt-5.3-codex':           { input: 1.75, output: 14.00, cache_write: 1.75, cache_read: 0.175 },
  'gpt-5.3-codex-spark':     { input: 1.75, output: 14.00, cache_write: 1.75, cache_read: 0.175 },
  'gpt-5.1-codex':           { input: 1.75, output: 14.00, cache_write: 1.75, cache_read: 0.175 },
  // Google (opencode) — ≤200k context tier; >200k context is roughly 2× input/output
  'gemini-3.1-pro-preview':  { input: 2.00, output: 12.00, cache_write: 2.00, cache_read: 0.20 },
  // Moonshot (opencode) — cache_write billed at standard input rate (no separate write SKU)
  'kimi-k2.6':               { input: 0.95, output:  4.00, cache_write: 0.95, cache_read: 0.16 },
};

function bareModel(model) {
  // opencode stores "<provider>/<model>"; strip the provider for lookup.
  return model && model.includes('/') ? model.split('/', 2)[1] : model;
}

function getPricing(model) {
  if (!model) return null;
  const bare = bareModel(model);
  for (const candidate of [model, bare]) {
    if (PRICING[candidate]) return PRICING[candidate];
    for (const key of Object.keys(PRICING)) {
      if (candidate.startsWith(key)) return PRICING[key];
    }
  }
  // Family fallback for unrecognised Claude variants
  const m = (bare || '').toLowerCase();
  if (m.includes('fable'))  return PRICING['claude-fable-5'];
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function isBillable(model) {
  // OpenRouter models always have an authoritative cost reported per session,
  // so we mark them billable even when no token-rate pricing exists.
  if (model && model.startsWith('openrouter/')) return true;
  return getPricing(model) !== null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// Per-session cost: OpenRouter (and any other source that reports authoritative
// $) populates actual_cost; everything else falls back to token-based pricing.
function sessionCost(s) {
  if (s.actual_cost != null) return s.actual_cost;
  return calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
}

function sessionHasCost(s) {
  return s.actual_cost != null || isBillable(s.model);
}

function modelCost(m) {
  return m.actual_cost != null
    ? m.actual_cost
    : calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
}

function modelHasCost(m) {
  return m.actual_cost != null || isBillable(m.model);
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = {
  '3hr':  'Last 3 Hours',
  '6hr':  'Last 6 Hours',
  '12hr': 'Last 12 Hours',
  '1d':   'Last 24 Hours',
  '3d':   'Last 3 Days',
  '7d':   'Last 7 Days',
  '30d':  'Last 30 Days',
  '90d':  'Last 90 Days',
  'all':  'All Time',
};
const RANGE_TICKS = {
  '3hr': 1, '6hr': 1, '12hr': 1, '1d': 2, '3d': 3,
  '7d': 7, '30d': 15, '90d': 13, 'all': 12,
};
const RANGE_HOURS = { '3hr': 3, '6hr': 6, '12hr': 12, '1d': 24 };
const RANGE_DAYS  = { '3d': 3, '7d': 7, '30d': 30, '90d': 90 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

// Returns "YYYY-MM-DD HH:MM" cutoff (local time) or null for 'all'.
function getRangeCutoff(range) {
  if (range === 'all') return null;
  const d = new Date();
  if (RANGE_HOURS[range]) {
    d.setHours(d.getHours() - RANGE_HOURS[range]);
  } else if (RANGE_DAYS[range]) {
    d.setDate(d.getDate() - RANGE_DAYS[range]);
    d.setHours(0, 0, 0, 0);
  }
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${m}">
      <input type="checkbox" value="${m}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${m}
    </label>`;
  }).join('');
}

function readURLProviders(allProviders) {
  const param = new URLSearchParams(window.location.search).get('providers');
  if (!param) return new Set(allProviders);
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allProviders.filter(p => fromURL.has(p)));
}

function isDefaultProviderSelection(allProviders) {
  if (selectedProviders.size !== allProviders.length) return false;
  return allProviders.every(p => selectedProviders.has(p));
}

function buildProviderUI(allProviders) {
  selectedProviders = readURLProviders(allProviders);
  const container = document.getElementById('provider-checkboxes');
  container.innerHTML = allProviders.map(p => {
    const checked = selectedProviders.has(p);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-provider="${p}">
      <input type="checkbox" value="${p}" ${checked ? 'checked' : ''} onchange="onProviderToggle(this)">
      ${p}
    </label>`;
  }).join('');
}

function onProviderToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedProviders.add(cb.value);    label.classList.add('checked'); }
  else            { selectedProviders.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  const allProviders = Array.from(document.querySelectorAll('#provider-checkboxes input')).map(cb => cb.value);
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  if (!isDefaultProviderSelection(allProviders)) params.set('providers', Array.from(selectedProviders).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const cutoff = getRangeCutoff(selectedRange);

  // Daily rows are bucketed per UTC day; compare just the date portion of cutoff.
  const cutoffDay = cutoff ? cutoff.slice(0, 10) : null;
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && selectedProviders.has(r.provider) && (!cutoffDay || r.day >= cutoffDay)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Sessions get minute-precision filtering (s.last is "YYYY-MM-DD HH:MM").
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && selectedProviders.has(s.provider) && (!cutoff || s.last >= cutoff)
  );

  // Add session counts and authoritative cost (OpenRouter etc.) into modelMap
  for (const s of filteredSessions) {
    if (!modelMap[s.model]) continue;
    modelMap[s.model].sessions++;
    if (s.actual_cost != null) {
      modelMap[s.model].actual_cost = (modelMap[s.model].actual_cost || 0) + s.actual_cost;
    }
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, turns: 0 };
    projMap[s.project].input  += s.input;
    projMap[s.project].output += s.output;
    projMap[s.project].turns  += s.turns;
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + modelCost(m), 0),
  };

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  lastFilteredSessions = filteredSessions;
  sessionsPage = 0;
  renderSessionsPage();
  renderModelCostTable(byModel);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Jul 2026', color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${s.value}</div>
      ${s.sub ? `<div class="sub">${s.sub}</div>` : ''}
    </div>
  `).join('');
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#1a1d27' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderSessionsPage() {
  const total = lastFilteredSessions.length;
  const pages = Math.max(1, Math.ceil(total / SESSIONS_PAGE_SIZE));
  if (sessionsPage >= pages) sessionsPage = pages - 1;
  if (sessionsPage < 0) sessionsPage = 0;
  const start = sessionsPage * SESSIONS_PAGE_SIZE;
  const slice = lastFilteredSessions.slice(start, start + SESSIONS_PAGE_SIZE);

  document.getElementById('sessions-body').innerHTML = slice.map(s => {
    const cost = sessionCost(s);
    const costCell = sessionHasCost(s)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const provClass = s.provider === 'opencode' ? 'oc'
                    : s.provider === 'OpenRouter' ? 'or'
                    : 'cc';
    return `<tr>
      <td class="muted" style="font-family:monospace">${s.session_id}${s.session_id.includes('-') ? '' : '&hellip;'}</td>
      <td><span class="provider-tag ${provClass}">${s.provider}</span></td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td><span class="model-tag">${s.model}</span></td>
      <td class="num">${fmt(s.turns)}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');

  const shownStart = total === 0 ? 0 : start + 1;
  const shownEnd = Math.min(start + SESSIONS_PAGE_SIZE, total);
  document.getElementById('sessions-pager').innerHTML = `
    <span class="page-info">${shownStart.toLocaleString()}–${shownEnd.toLocaleString()} of ${total.toLocaleString()}</span>
    <button onclick="sessionsPageStep(-1)" ${sessionsPage === 0 ? 'disabled' : ''}>Prev</button>
    <button onclick="sessionsPageStep(1)"  ${sessionsPage >= pages - 1 ? 'disabled' : ''}>Next</button>
  `;
}

function sessionsPageStep(delta) {
  sessionsPage += delta;
  renderSessionsPage();
}

function renderModelCostTable(byModel) {
  const sorted = [...byModel].sort((a, b) => modelCost(b) - modelCost(a));
  document.getElementById('model-cost-body').innerHTML = sorted.map(m => {
    const cost = m.actual_cost != null
      ? m.actual_cost
      : calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const hasCost = m.actual_cost != null || isBillable(m.model);
    const costCell = hasCost
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${m.model}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + d.error + '</div>';
      return;
    }
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + ' \u00b7 Auto-refresh in 30s';

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Build provider + model filters (each reads URL for its own selection)
      buildProviderUI(d.all_providers || []);
      buildFilterUI(d.all_models);
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


class DashboardServer(HTTPServer):
    allow_reuse_address = True


def serve(port=8080):
    server = DashboardServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
