"""
cli.py - Command-line interface for the AI usage dashboard.

Commands:
  scan      - Scan local/remote usage sources and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import sys
import argparse
import errno
import sqlite3
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path.home() / ".claude" / "usage.db"

PRICING = {
    "claude-fable-5":          {"input": 10.00, "output": 50.00},
    "claude-opus-4-8":         {"input":  5.00, "output": 25.00},
    "claude-opus-4-7":         {"input":  5.00, "output": 25.00},
    "claude-opus-4-6":         {"input":  5.00, "output": 25.00},
    "claude-opus-4-5":         {"input":  5.00, "output": 25.00},
    "claude-sonnet-4-6":       {"input":  3.00, "output": 15.00},
    "claude-sonnet-4-5":       {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":        {"input":  1.00, "output":  5.00},
    # opencode-surfaced models (approximate API pricing per 1M tokens)
    "gpt-5.5":                 {"input":  5.00, "output": 20.00},
    "gpt-5.4":                 {"input":  2.50, "output": 10.00},
    "gpt-5.4-mini":            {"input":  0.20, "output":  0.80},
    "gpt-5.3-codex":           {"input":  5.00, "output": 20.00},
    "gpt-5.3-codex-spark":     {"input":  5.00, "output": 20.00},
    "gpt-5.1-codex":           {"input":  2.50, "output": 10.00},
    "gemini-3.1-pro-preview":  {"input":  3.50, "output": 14.00},
    "kimi-k2.6":               {"input":  0.15, "output":  2.50},
    "default":                 {"input":  3.00, "output": 15.00},
}

def get_pricing(model):
    if not model:
        return PRICING["default"]
    # opencode emits "<provider>/<model>"; pricing keys are bare model names.
    bare = model.split("/", 1)[1] if "/" in model else model
    for candidate in (model, bare):
        if candidate in PRICING:
            return PRICING[candidate]
        for key in PRICING:
            if key != "default" and candidate.startswith(key):
                return PRICING[key]
    return PRICING["default"]

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    return (
        inp          * p["input"]  / 1_000_000 +
        out          * p["output"] / 1_000_000 +
        cache_read   * p["input"]  * 0.10 / 1_000_000 +
        cache_creation * p["input"] * 1.25 / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:,.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(args=None):
    from scanner import scan, PROJECTS_DIR
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()

    from codex_scanner import scan as codex_scan, CODEX_SESSIONS_DIR
    print(f"\nScanning {CODEX_SESSIONS_DIR} ...")
    codex_scan()
    from pi_scanner import scan as pi_scan, SESSION_DIRS as PI_SESSION_DIRS
    print(f"\nScanning Oh My Pi sessions ({', '.join(str(p) for p in PI_SESSION_DIRS)}) ...")
    pi_scan()


    from opencode_scanner import scan as oc_scan, OPENCODE_DB_PATH
    print(f"\nScanning {OPENCODE_DB_PATH} ...")
    oc_scan()

    from openrouter_scanner import scan as or_scan, API_URL as OR_API_URL
    print(f"\nFetching {OR_API_URL} ...")
    or_scan()

    cmd_sync_pricing()


def cmd_sync_pricing(args=None):
    """Refresh model pricing from models.dev (best-effort; never fatal)."""
    refresh = bool(getattr(args, "refresh", False))
    try:
        from pricing_sync import generate, _db_bare_models, PRICING_PATH
        from scanner import DB_PATH
        import json as _json

        names = _db_bare_models(DB_PATH) if Path(DB_PATH).exists() else set()
        print("\nSyncing model pricing from models.dev ...")
        pricing, unresolved = generate(names, refresh=refresh)
        PRICING_PATH.write_text(_json.dumps(pricing, indent=2, sort_keys=True) + "\n")
        print(f"  Priced {len(pricing)} models -> {PRICING_PATH.name}"
              + (f" ({len(unresolved)} unresolved, using built-in fallback)" if unresolved else ""))
    except Exception as e:
        print(f"  Pricing sync skipped: {e}")


def cmd_today(args=None):
    conn = require_db()
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4,}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4,}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']:,}")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats(args=None):
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # All-time totals
    totals = conn.execute("""
        SELECT
            SUM(total_input_tokens)   as inp,
            SUM(total_output_tokens)  as out,
            SUM(total_cache_read)     as cr,
            SUM(total_cache_creation) as cc,
            SUM(turn_count)           as turns,
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # By model
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(total_input_tokens)    as inp,
            SUM(total_output_tokens)   as out,
            SUM(total_cache_read)      as cr,
            SUM(total_cache_creation)  as cc,
            SUM(turn_count)            as turns,
            COUNT(*)                   as sessions
        FROM sessions
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects
    top_projects = conn.execute("""
        SELECT
            project_name,
            SUM(total_input_tokens)  as inp,
            SUM(total_output_tokens) as out,
            SUM(turn_count)          as turns,
            COUNT(*)                 as sessions
        FROM sessions
        GROUP BY project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out,
            AVG(daily_cost) as avg_cost
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out,
                0.0 as daily_cost
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  AI Usage - All-Time Statistics")
    hr("=")

    first_date = (totals["first"] or "")[:10]
    last_date = (totals["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {totals['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print()
    print(f"  Est. total cost:  {fmt_cost(total_cost)}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4,}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3,}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(args=None):
    import webbrowser
    import threading
    import time

    port = args.port if args else 8080

    print("Running scan first...")
    cmd_scan()

    print("\nStarting dashboard server...")
    from dashboard import serve

    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://localhost:{port}")

    if not args or not args.no_open:
        t = threading.Thread(target=open_browser, daemon=True)
        t.start()

    try:
        serve(port=port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"Port {port} is already in use. Try: python cli.py dashboard --port {port + 1}")
            sys.exit(1)
        raise


# ── Entry point ───────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(description="AI Usage Dashboard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan all sources and update database")
    scan_parser.set_defaults(func=cmd_scan)

    today_parser = subparsers.add_parser("today", help="Show today's usage summary")
    today_parser.set_defaults(func=cmd_today)

    stats_parser = subparsers.add_parser("stats", help="Show all-time statistics")
    stats_parser.set_defaults(func=cmd_stats)

    pricing_parser = subparsers.add_parser(
        "sync-pricing", help="Refresh model pricing from models.dev"
    )
    pricing_parser.add_argument(
        "--refresh", action="store_true", help="Force re-download of the models.dev catalog"
    )
    pricing_parser.set_defaults(func=cmd_sync_pricing)

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Scan and start dashboard server",
        description="Scan all sources and start the dashboard server.",
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to serve the dashboard on (default: 8080)",
    )
    dashboard_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the dashboard in a browser",
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    return parser

if __name__ == "__main__":
    parsed_args = build_parser().parse_args()
    parsed_args.func(parsed_args)
