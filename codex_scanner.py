"""
codex_scanner.py - Scans Codex JSONL rollout files and stores token usage in
the same usage.db used for the other local agent sources.
"""

import glob
import json
import os
import re
from pathlib import Path

from scanner import (
    DB_PATH,
    get_db,
    init_db,
    insert_turns,
    project_name_from_cwd,
    upsert_sessions,
)

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
UUID_AT_END = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_id_from_path(filepath):
    match = UUID_AT_END.search(Path(filepath).stem)
    return match.group(1) if match else Path(filepath).stem


def _full_model(model, provider):
    if not model:
        return ""
    if "/" in model or not provider:
        return model
    return f"{provider}/{model}"


def _codex_source(meta):
    source = meta.get("source")
    originator = meta.get("originator")
    thread_source = meta.get("thread_source")

    if thread_source == "subagent" or (
        isinstance(source, dict) and "subagent" in source
    ):
        return "codex-subagent"
    if source == "cli" or originator == "codex-tui":
        return "codex-cli"
    if source == "exec" or originator == "codex_exec":
        return "codex-exec"
    if isinstance(source, str) and source:
        safe = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        return f"codex-{safe}" if safe else "codex"
    return "codex"


def _git_branch(meta):
    git = meta.get("git")
    if isinstance(git, dict):
        return git.get("branch") or ""
    return ""


def _tool_name(payload):
    ptype = payload.get("type")
    if ptype == "function_call":
        return payload.get("name") or "function_call"
    if ptype == "tool_search_call":
        return "tool_search"
    if ptype == "web_search_call":
        return "web_search"
    if isinstance(ptype, str) and ptype.endswith("_call"):
        return ptype[:-5]
    return None


def _turn_from_token_count(session_id, timestamp, usage, model, cwd, tool_name):
    input_total = _to_int(usage.get("input_tokens"))
    cached_input = _to_int(usage.get("cached_input_tokens"))
    output = _to_int(usage.get("output_tokens"))
    reasoning = _to_int(usage.get("reasoning_output_tokens"))

    if input_total + cached_input + output + reasoning == 0:
        return None

    return {
        "session_id": session_id,
        # Codex input_tokens includes cached input. Store uncached input and the
        # cached subset separately so charts and costs do not double-count it.
        "timestamp": timestamp,
        "model": model,
        "input_tokens": max(input_total - cached_input, 0),
        "output_tokens": output + reasoning,
        "cache_read_tokens": cached_input,
        "cache_creation_tokens": 0,
        "tool_name": tool_name,
        "cwd": cwd,
    }


def parse_jsonl_file(filepath):
    """Return (session_meta, turns) for one Codex rollout JSONL."""
    session_meta = {}
    turns = []

    session_id = _session_id_from_path(filepath)
    first_timestamp = ""
    last_timestamp = ""
    current_model = ""
    current_cwd = ""
    pending_tool_name = None

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

                timestamp = record.get("timestamp") or ""
                if timestamp:
                    if not first_timestamp or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if not last_timestamp or timestamp > last_timestamp:
                        last_timestamp = timestamp

                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue

                rtype = record.get("type")
                if rtype == "session_meta":
                    session_meta = payload
                    session_id = (
                        payload.get("session_id")
                        or payload.get("id")
                        or session_id
                    )
                    current_cwd = payload.get("cwd") or current_cwd
                    provider = payload.get("model_provider") or ""
                    current_model = _full_model(current_model, provider)
                    meta_ts = payload.get("timestamp") or ""
                    if meta_ts and (not first_timestamp or meta_ts < first_timestamp):
                        first_timestamp = meta_ts
                    if meta_ts and (not last_timestamp or meta_ts > last_timestamp):
                        last_timestamp = meta_ts
                    continue

                if rtype == "turn_context":
                    provider = session_meta.get("model_provider") or ""
                    current_model = _full_model(payload.get("model") or current_model, provider)
                    current_cwd = payload.get("cwd") or current_cwd
                    continue

                if rtype == "response_item":
                    tool_name = _tool_name(payload)
                    if tool_name and pending_tool_name is None:
                        pending_tool_name = tool_name
                    continue

                if rtype != "event_msg" or payload.get("type") != "token_count":
                    continue

                info = payload.get("info") or {}
                usage = info.get("last_token_usage") or {}
                if not isinstance(usage, dict):
                    continue

                provider = session_meta.get("model_provider") or ""
                model = _full_model(current_model, provider)
                turn = _turn_from_token_count(
                    session_id=session_id,
                    timestamp=timestamp,
                    usage=usage,
                    model=model,
                    cwd=current_cwd,
                    tool_name=pending_tool_name,
                )
                pending_tool_name = None
                if turn:
                    turns.append(turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")
        return None, []

    if not turns:
        return None, []

    if not current_cwd:
        current_cwd = next((t["cwd"] for t in turns if t["cwd"]), "")
    if not current_model:
        current_model = next((t["model"] for t in reversed(turns) if t["model"]), "")

    meta = {
        "session_id": session_id,
        "project_name": project_name_from_cwd(current_cwd),
        "first_timestamp": first_timestamp or turns[0]["timestamp"],
        "last_timestamp": last_timestamp or turns[-1]["timestamp"],
        "git_branch": _git_branch(session_meta),
        "model": current_model,
        "total_input_tokens": sum(t["input_tokens"] for t in turns),
        "total_output_tokens": sum(t["output_tokens"] for t in turns),
        "total_cache_read": sum(t["cache_read_tokens"] for t in turns),
        "total_cache_creation": 0,
        "turn_count": len(turns),
        "source": _codex_source(session_meta),
    }
    return meta, turns


def _line_count(filepath):
    with open(filepath, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def scan(sessions_dir=CODEX_SESSIONS_DIR, db_path=DB_PATH, verbose=True):
    if not Path(sessions_dir).exists():
        if verbose:
            print(f"  Codex sessions dir not found at {sessions_dir}, skipping.")
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    conn = get_db(db_path)
    init_db(conn)

    jsonl_files = glob.glob(str(Path(sessions_dir) / "**" / "*.jsonl"), recursive=True)
    jsonl_files.sort()

    new_files = updated_files = skipped_files = 0
    turns_added = 0
    sessions_seen = 0

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

        meta, turns = parse_jsonl_file(filepath)
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
        print(f"\nCodex scan complete:")
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
    print(f"Scanning {CODEX_SESSIONS_DIR} ...")
    scan()
