"""
opencode_scanner.py - Scans opencode's SQLite database and stores session/turn
data into the same usage.db used for Claude Code transcripts.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from scanner import (
    DB_PATH,
    get_db,
    init_db,
    project_name_from_cwd,
    upsert_sessions,
    insert_turns,
)

OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _iso(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _open_opencode_db(path):
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _processed_key(session_id):
    return f"opencode://{session_id}"


def _collect_session(oc_conn, session_row):
    """Return (session_meta, turns) for a single opencode session."""
    session_id = session_row["id"]
    directory = session_row["directory"] or ""
    project = project_name_from_cwd(directory)

    turns = []
    model_seen = None
    first_ts = None
    last_ts = None

    msg_rows = oc_conn.execute(
        "SELECT data, time_created FROM message WHERE session_id = ? ORDER BY time_created",
        (session_id,),
    ).fetchall()

    for m in msg_rows:
        try:
            data = json.loads(m["data"])
        except json.JSONDecodeError:
            continue
        if data.get("role") != "assistant":
            continue

        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        inp = int(tokens.get("input") or 0)
        out = int(tokens.get("output") or 0)
        reasoning = int(tokens.get("reasoning") or 0)
        cache_read = int(cache.get("read") or 0)
        cache_write = int(cache.get("write") or 0)

        if inp + out + reasoning + cache_read + cache_write == 0:
            continue

        provider = data.get("providerID") or ""
        model = data.get("modelID") or ""
        full_model = f"{provider}/{model}" if provider and model else (model or provider or "")

        ts_ms = (data.get("time") or {}).get("created") or m["time_created"]
        ts = _iso(ts_ms)
        if not first_ts or ts < first_ts:
            first_ts = ts
        if not last_ts or ts > last_ts:
            last_ts = ts

        cwd = ((data.get("path") or {}).get("cwd")) or directory
        model_seen = full_model

        turns.append({
            "session_id": session_id,
            "timestamp": ts,
            "model": full_model,
            "input_tokens": inp,
            # opencode bills reasoning as output; Anthropic-style schema only has
            # input/output/cache, so fold reasoning into output.
            "output_tokens": out + reasoning,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_write,
            "tool_name": None,
            "cwd": cwd,
        })

    if not turns:
        return None, []

    meta = {
        "session_id": session_id,
        "project_name": project,
        "first_timestamp": first_ts or _iso(session_row["time_created"]),
        "last_timestamp": last_ts or _iso(session_row["time_updated"]),
        "git_branch": "",
        "model": model_seen,
        "total_input_tokens": sum(t["input_tokens"] for t in turns),
        "total_output_tokens": sum(t["output_tokens"] for t in turns),
        "total_cache_read": sum(t["cache_read_tokens"] for t in turns),
        "total_cache_creation": sum(t["cache_creation_tokens"] for t in turns),
        "turn_count": len(turns),
        "source": "opencode",
    }
    return meta, turns


def scan(opencode_db=OPENCODE_DB_PATH, db_path=DB_PATH, verbose=True):
    if not Path(opencode_db).exists():
        if verbose:
            print(f"  opencode db not found at {opencode_db}, skipping.")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    conn = get_db(db_path)
    init_db(conn)

    try:
        oc = _open_opencode_db(opencode_db)
    except sqlite3.OperationalError as e:
        print(f"  Could not open opencode db: {e}")
        conn.close()
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    sessions = oc.execute(
        "SELECT id, directory, time_created, time_updated FROM session "
        "ORDER BY time_updated"
    ).fetchall()

    new_count = updated_count = skipped_count = 0
    turns_added = 0
    sessions_seen = 0

    for s in sessions:
        sid = s["id"]
        key = _processed_key(sid)

        msg_count_row = oc.execute(
            "SELECT COUNT(*) AS c FROM message WHERE session_id = ?", (sid,)
        ).fetchone()
        msg_count = msg_count_row["c"] if msg_count_row else 0

        existing = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?", (key,)
        ).fetchone()

        # mtime stored as the opencode session's time_updated (epoch ms)
        if existing and existing["mtime"] == s["time_updated"] and existing["lines"] == msg_count:
            skipped_count += 1
            continue

        is_new = existing is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] opencode session {sid[:12]}…  ({msg_count} messages)")

        meta, turns = _collect_session(oc, s)
        if meta is None:
            # Empty session, just record it as processed so we don't re-check.
            conn.execute(
                "INSERT OR REPLACE INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
                (key, s["time_updated"], msg_count),
            )
            conn.commit()
            continue

        # Wipe any prior turns for this session so we can re-insert idempotently.
        conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

        upsert_sessions(conn, [meta])
        insert_turns(conn, turns)
        conn.execute(
            "INSERT OR REPLACE INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
            (key, s["time_updated"], msg_count),
        )
        conn.commit()

        turns_added += len(turns)
        sessions_seen += 1
        if is_new:
            new_count += 1
        else:
            updated_count += 1

    oc.close()
    conn.close()

    if verbose:
        print(f"\nopencode scan complete:")
        print(f"  New sessions:     {new_count}")
        print(f"  Updated sessions: {updated_count}")
        print(f"  Skipped sessions: {skipped_count}")
        print(f"  Turns added:      {turns_added}")
        print(f"  Sessions seen:    {sessions_seen}")

    return {"new": new_count, "updated": updated_count, "skipped": skipped_count,
            "turns": turns_added, "sessions": sessions_seen}


if __name__ == "__main__":
    print(f"Scanning {OPENCODE_DB_PATH} ...")
    scan()
