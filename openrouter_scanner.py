"""
openrouter_scanner.py - Pulls usage rollups from OpenRouter's /api/v1/activity
endpoint and stores them in usage.db.

OpenRouter only exposes daily, per-(model, endpoint) aggregates for the last
30 days, so each (date, model, provider) row becomes a single synthetic
"session" with one synthetic "turn" timestamped at midnight UTC. We trust
OpenRouter's reported USD cost (paid + BYOK inference) and stash it in the
sessions.actual_cost_usd column instead of computing one from token counts.

Requires the env var OPENROUTER_API_KEY to be set to a key with
analytics-read scope (typically a "provisioning"/management key).
"""

import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from scanner import DB_PATH, get_db, init_db, upsert_sessions, insert_turns

API_URL = "https://openrouter.ai/api/v1/activity"
ENV_KEY = "OPENROUTER_API_KEY"
DOTENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_dotenv(path=DOTENV_PATH):
    """Minimal .env loader: KEY=VALUE per line, # comments and blanks ignored.
    Existing environment variables win."""
    if not Path(path).exists():
        return
    try:
        for raw in Path(path).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def _session_id(date, model_slug, endpoint_id):
    # endpoint_id disambiguates the same model served by two providers on the
    # same day (OpenRouter rolls these up separately).
    suffix = f":{endpoint_id}" if endpoint_id else ""
    return f"openrouter:{date}:{model_slug}{suffix}"


def _fetch_activity(api_key, timeout=20):
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "claude-usage/openrouter-scanner",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _row_to_session_and_turn(row):
    raw_date = row.get("date")
    if not raw_date:
        return None, None
    # OpenRouter sometimes returns "YYYY-MM-DD" and sometimes "YYYY-MM-DD HH:MM:SS";
    # we only want the date part for grouping and the synthetic timestamp.
    date = str(raw_date)[:10]
    model = row.get("model") or row.get("model_permaslug") or "unknown"
    provider = row.get("provider_name") or "unknown"
    endpoint_id = row.get("endpoint_id") or ""

    prompt = int(row.get("prompt_tokens") or 0)
    completion = int(row.get("completion_tokens") or 0)
    reasoning = int(row.get("reasoning_tokens") or 0)
    requests = int(row.get("requests") or 0)
    paid_usage = float(row.get("usage") or 0.0)
    byok_usage = float(row.get("byok_usage_inference") or 0.0)
    cost = paid_usage + byok_usage

    if prompt + completion + reasoning + requests == 0 and cost == 0:
        return None, None

    sid = _session_id(date, model, endpoint_id)
    full_model = f"openrouter/{model}"
    ts = f"{date}T00:00:00Z"

    turn = {
        "session_id": sid,
        "timestamp": ts,
        "model": full_model,
        "input_tokens": prompt,
        # OpenRouter bills reasoning as completion-side; fold into output.
        "output_tokens": completion + reasoning,
        # /activity does not break out cache reads/writes.
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "tool_name": None,
        "cwd": "",
    }
    meta = {
        "session_id": sid,
        "project_name": "OpenRouter session",
        "first_timestamp": ts,
        "last_timestamp": ts,
        "git_branch": "",
        "model": full_model,
        "total_input_tokens": prompt,
        "total_output_tokens": completion + reasoning,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": requests or 1,
        "source": "openrouter",
        "actual_cost_usd": cost,
    }
    return meta, turn


def scan(api_key=None, db_path=DB_PATH, verbose=True):
    _load_dotenv()
    api_key = api_key or os.environ.get(ENV_KEY)
    if not api_key:
        if verbose:
            print(f"  ${ENV_KEY} not set, skipping OpenRouter scan.")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    try:
        payload = _fetch_activity(api_key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  OpenRouter API error {e.code}: {body}")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  OpenRouter request failed: {e}")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    rows = payload.get("data") or []

    conn = get_db(db_path)
    init_db(conn)

    new_count = updated_count = 0
    turns_added = 0

    for row in rows:
        meta, turn = _row_to_session_and_turn(row)
        if meta is None:
            continue

        sid = meta["session_id"]
        existed = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()

        # Each (date, model, endpoint) row in /activity is a complete daily
        # snapshot — wipe and reinsert so today's growing numbers stay correct.
        conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

        upsert_sessions(conn, [meta])
        insert_turns(conn, [turn])
        turns_added += 1
        if existed:
            updated_count += 1
        else:
            new_count += 1

    conn.commit()
    conn.close()

    if verbose:
        print(f"\nOpenRouter scan complete:")
        print(f"  New rollups:      {new_count}")
        print(f"  Updated rollups:  {updated_count}")
        print(f"  Total rows seen:  {len(rows)}")

    return {"new": new_count, "updated": updated_count, "skipped": 0,
            "turns": turns_added, "sessions": new_count + updated_count}


if __name__ == "__main__":
    print(f"Fetching {API_URL} ...")
    scan()
