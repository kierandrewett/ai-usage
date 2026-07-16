"""
pi_scanner.py - Scans Oh My Pi JSONL session files and stores token usage in
usage.db alongside Claude Code, Codex, opencode, and OpenRouter data.
"""

import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scanner import (
    DB_PATH,
    get_db,
    init_db,
    insert_turns,
    project_name_from_cwd,
    upsert_sessions,
)

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
OMP_SESSIONS_DIR = Path.home() / ".omp" / "agent" / "sessions"
SESSION_DIRS = (PI_SESSIONS_DIR, OMP_SESSIONS_DIR)


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_from_timestamp(value):
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    # Session messages store milliseconds since epoch.
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace("+00:00", "Z")


def _full_model(model, provider):
    if not model:
        return ""
    if "/" in model or not provider:
        return model
    return f"{provider}/{model}"


def _provider_from_model(model):
    if isinstance(model, str) and "/" in model:
        return model.split("/", 1)[0]
    return ""


def _usage_int(usage, *keys):
    for key in keys:
        if key in usage:
            return _to_int(usage.get(key))
    return 0


def _usage_cost(usage):
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None

    total = _to_float(cost.get("total"))
    if total is not None:
        return total

    parts = [
        _to_float(cost.get("input")),
        _to_float(cost.get("output")),
        _to_float(cost.get("cacheRead")),
        _to_float(cost.get("cache_read")),
        _to_float(cost.get("cacheWrite")),
        _to_float(cost.get("cache_write")),
    ]
    present = [part for part in parts if part is not None]
    return sum(present) if present else None


def _tool_name(message):
    for item in message.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("toolCall", "tool_use"):
            return item.get("name")
    return None


def _source_for_path(filepath, sessions_dir):
    path = Path(filepath)
    root = Path(sessions_dir)
    if path.name == "__advisor.jsonl":
        return "oh-my-pi-advisor"
    try:
        direct_session = path.parent.parent == root
    except RuntimeError:
        direct_session = False
    return "oh-my-pi" if direct_session else "oh-my-pi-subagent"


def parse_jsonl_file(filepath, sessions_dir=None):
    """Return (session_meta, turns) for one Oh My Pi session JSONL."""
    filepath = Path(filepath)
    session_id = filepath.stem
    current_cwd = ""
    current_model = ""
    git_branch = ""
    first_timestamp = ""
    last_timestamp = ""
    turns = []
    actual_cost = 0.0
    has_actual_cost = False

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = _iso_from_timestamp(record.get("timestamp"))
                if timestamp:
                    if not first_timestamp or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if not last_timestamp or timestamp > last_timestamp:
                        last_timestamp = timestamp

                rtype = record.get("type")
                if rtype == "session":
                    session_id = record.get("id") or session_id
                    current_cwd = record.get("cwd") or current_cwd
                    header_ts = _iso_from_timestamp(record.get("timestamp"))
                    if header_ts:
                        if not first_timestamp or header_ts < first_timestamp:
                            first_timestamp = header_ts
                        if not last_timestamp or header_ts > last_timestamp:
                            last_timestamp = header_ts
                    continue

                if rtype == "model_change":
                    current_model = record.get("model") or current_model
                    continue

                if rtype != "message":
                    continue

                message = record.get("message") or {}
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue

                usage = message.get("usage") or {}
                if not isinstance(usage, dict):
                    continue

                provider = message.get("provider") or _provider_from_model(current_model)
                model = _full_model(message.get("model") or current_model, provider)
                if model:
                    current_model = model

                input_tokens = _usage_int(usage, "input", "input_tokens")
                output_tokens = _usage_int(usage, "output", "output_tokens")
                cache_read = _usage_int(
                    usage,
                    "cacheRead",
                    "cache_read",
                    "cache_read_tokens",
                    "cache_read_input_tokens",
                )
                cache_write = _usage_int(
                    usage,
                    "cacheWrite",
                    "cache_write",
                    "cache_creation",
                    "cache_creation_tokens",
                    "cache_creation_input_tokens",
                )

                cost = _usage_cost(usage)
                if cost is not None:
                    actual_cost += cost
                    has_actual_cost = True

                if input_tokens + output_tokens + cache_read + cache_write == 0:
                    continue

                msg_timestamp = timestamp or _iso_from_timestamp(message.get("timestamp"))
                if msg_timestamp:
                    if not first_timestamp or msg_timestamp < first_timestamp:
                        first_timestamp = msg_timestamp
                    if not last_timestamp or msg_timestamp > last_timestamp:
                        last_timestamp = msg_timestamp

                turns.append({
                    "session_id": session_id,
                    "timestamp": msg_timestamp,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_write,
                    "tool_name": _tool_name(message),
                    "cwd": current_cwd,
                })

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")
        return None, []

    if not turns:
        return None, []

    if not current_model:
        current_model = next((t["model"] for t in reversed(turns) if t["model"]), "")

    meta = {
        "session_id": session_id,
        "project_name": project_name_from_cwd(current_cwd),
        "first_timestamp": first_timestamp or turns[0]["timestamp"],
        "last_timestamp": last_timestamp or turns[-1]["timestamp"],
        "git_branch": git_branch,
        "model": current_model,
        "total_input_tokens": sum(t["input_tokens"] for t in turns),
        "total_output_tokens": sum(t["output_tokens"] for t in turns),
        "total_cache_read": sum(t["cache_read_tokens"] for t in turns),
        "total_cache_creation": sum(t["cache_creation_tokens"] for t in turns),
        "turn_count": len(turns),
        "source": _source_for_path(filepath, sessions_dir or filepath.parent),
        "actual_cost_usd": actual_cost if has_actual_cost else None,
    }
    return meta, turns


def _line_count(filepath):
    with open(filepath, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def _session_dirs(session_dirs):
    if isinstance(session_dirs, (str, os.PathLike)):
        return [Path(session_dirs)]
    return [Path(p) for p in session_dirs]


def scan(session_dirs=SESSION_DIRS, db_path=DB_PATH, verbose=True):
    dirs = _session_dirs(session_dirs)
    existing_dirs = [d for d in dirs if d.exists()]
    if not existing_dirs:
        if verbose:
            formatted = " or ".join(str(d) for d in dirs)
            print(f"  Oh My Pi sessions dir not found at {formatted}, skipping.")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    conn = get_db(db_path)
    init_db(conn)

    new_files = updated_files = skipped_files = 0
    turns_added = 0
    sessions_seen = 0

    for sessions_dir in existing_dirs:
        jsonl_files = glob.glob(str(sessions_dir / "**" / "*.jsonl"), recursive=True)
        jsonl_files.sort()

        for filepath in jsonl_files:
            try:
                mtime = os.path.getmtime(filepath)
                lines = _line_count(filepath)
            except OSError:
                continue

            row = conn.execute(
                "SELECT mtime, lines FROM processed_files WHERE path = ?",
                (filepath,),
            ).fetchone()

            if row and abs(row["mtime"] - mtime) < 0.01 and row["lines"] == lines:
                skipped_files += 1
                continue

            is_new = row is None
            if verbose:
                status = "NEW" if is_new else "UPD"
                print(f"  [{status}] {os.path.relpath(filepath, sessions_dir)}")

            meta, turns = parse_jsonl_file(filepath, sessions_dir)
            if meta is not None:
                sid = meta["session_id"]
                conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                upsert_sessions(conn, [meta])
                insert_turns(conn, turns)
                turns_added += len(turns)
                sessions_seen += 1

            conn.execute(
                "INSERT OR REPLACE INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
                (filepath, mtime, lines),
            )
            conn.commit()

            if is_new:
                new_files += 1
            else:
                updated_files += 1

    conn.close()

    if verbose:
        print(f"\nOh My Pi scan complete:")
        print(f"  New files:       {new_files}")
        print(f"  Updated files:   {updated_files}")
        print(f"  Skipped files:   {skipped_files}")
        print(f"  Turns added:     {turns_added}")
        print(f"  Sessions seen:   {sessions_seen}")

    return {
        "new": new_files,
        "updated": updated_files,
        "skipped": skipped_files,
        "turns": turns_added,
        "sessions": sessions_seen,
    }


if __name__ == "__main__":
    print("Scanning Oh My Pi sessions ...")
    scan()
