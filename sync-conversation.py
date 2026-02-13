#!/usr/bin/env python3
"""
sync-conversation.py — Maintain search-friendly conversation transcripts.

Triggered by Claude Code hooks (Stop, PreCompact).
Reads the session JSONL, extracts human/assistant text (no tool blocks),
writes grep-friendly markdown to .conversations/sessions/.
Chained sessions (from claude -c) are merged into a single file per chain.

Uses tree-walking to follow only the active branch of the conversation,
correctly handling rewinds (where abandoned branches remain in the JSONL).
Each sync rewrites the full .md file; git captures the diff.

Works in two modes:
  - Global: installed at ~/.claude/hooks/, uses cwd from hook input
  - Per-project: installed at <project>/.claude/hooks/, uses script location
On first trigger in a new project, auto-creates .conversations/.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def get_project_root(hook_input):
    """Determine project root from hook input cwd, with script location fallback."""
    cwd = hook_input.get("cwd", "")
    if cwd and os.path.isdir(cwd):
        return Path(cwd)

    # Fallback: derive from script location (per-project install)
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def ensure_project_setup(project_root):
    """Auto-setup .conversations/ on first run in a project."""
    conv_dir = project_root / ".conversations"
    sessions_dir = conv_dir / "sessions"
    parts_dir = conv_dir / ".parts"
    state_dir = conv_dir / ".state"
    index_path = conv_dir / "index.md"

    # Create directory structure
    sessions_dir.mkdir(parents=True, exist_ok=True)
    parts_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Add internal dirs to .gitignore
    gitignore = conv_dir / ".gitignore"
    needed = [".state/", ".parts/"]
    if not gitignore.exists():
        gitignore.write_text("\n".join(needed) + "\n")
    else:
        content = gitignore.read_text()
        added = False
        for entry in needed:
            if entry not in content:
                content += entry + "\n"
                added = True
        if added:
            gitignore.write_text(content)

    # Initialize git repo
    if not (conv_dir / ".git").exists():
        subprocess.run(
            ["git", "init"], cwd=conv_dir, capture_output=True, timeout=5
        )
        if not index_path.exists():
            index_path.write_text("# Conversation Index\n\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=conv_dir, capture_output=True, timeout=5
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "Init conversation history"],
            cwd=conv_dir,
            capture_output=True,
            timeout=5,
        )
    elif not index_path.exists():
        index_path.write_text("# Conversation Index\n\n")


def find_transcript_path(hook_input):
    """Get transcript path from hook input, with fallback for bug #13668."""
    transcript_path = hook_input.get("transcript_path", "")
    if transcript_path and os.path.exists(transcript_path):
        return Path(transcript_path)

    # Fallback: search ~/.claude/projects/ by session_id
    session_id = hook_input.get("session_id", "")
    if not session_id:
        return None

    claude_dir = Path.home() / ".claude" / "projects"
    if claude_dir.exists():
        for jsonl_file in claude_dir.rglob(f"{session_id}.jsonl"):
            return jsonl_file

    return None


def extract_active_branch(jsonl_path):
    """Extract user/assistant text messages across all conversation segments.

    A single JSONL file can contain multiple disconnected trees (roots) due to
    compaction or `claude -c` creating new starting points. Each root defines
    a subtree. This function finds the active branch within each subtree and
    concatenates them in chronological order, giving the full conversation
    history that survives compaction.

    Returns (messages, file_size, active_leaf_uuid, first_ts, last_ts, orphan_parents).
    first_ts/last_ts are raw ISO timestamp strings.
    orphan_parents: list of parentUuids not found within this session (cross-session links).
    """
    file_size = jsonl_path.stat().st_size

    # Single pass: build tree and extract text for user/assistant entries
    entries = {}
    children = defaultdict(list)

    with open(jsonl_path, "r") as f:
        for line_num, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            uuid = entry.get("uuid", "")
            if not uuid:
                continue  # file-history-snapshot, summary, queue-operation

            entry_type = entry.get("type", "")
            parent_uuid = entry.get("parentUuid", "")
            is_sidechain = entry.get("isSidechain", False)
            raw_timestamp = entry.get("timestamp", "")

            # Extract text data for user/assistant entries
            text_data = None
            if entry_type in ("user", "assistant"):
                message = entry.get("message", {})
                content = message.get("content", [])

                if isinstance(content, str):
                    text_parts = [content]
                elif isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                text_parts.append(text)
                        elif isinstance(block, str):
                            text_parts.append(block)
                else:
                    text_parts = []

                if text_parts:
                    time_str = ""
                    if raw_timestamp:
                        try:
                            dt = datetime.fromisoformat(
                                raw_timestamp.replace("Z", "+00:00")
                            )
                            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, AttributeError):
                            time_str = raw_timestamp[:19]

                    text_data = {
                        "role": entry_type.capitalize(),
                        "text": "\n\n".join(text_parts),
                        "time": time_str,
                    }

            entries[uuid] = {
                "type": entry_type,
                "parentUuid": parent_uuid,
                "line": line_num,
                "isSidechain": is_sidechain,
                "text_data": text_data,
                "timestamp": raw_timestamp,
            }

            if parent_uuid:
                children[parent_uuid].append(uuid)

    if not entries:
        return [], file_size, None, None, None, []

    # Find orphan parentUuids (cross-session references from claude -c)
    all_uuids = set(entries.keys())
    orphan_parents = [
        entries[u]["parentUuid"]
        for u in entries
        if entries[u]["parentUuid"]
        and entries[u]["parentUuid"] not in all_uuids
        and not entries[u]["isSidechain"]
    ]

    # Find non-sidechain leaves
    leaves = [
        u
        for u in entries
        if u not in children and not entries[u]["isSidechain"]
    ]
    if not leaves:
        leaves = [u for u in entries if u not in children]
    if not leaves:
        return [], file_size, None, None, None, orphan_parents

    # Trace each leaf to its root to group leaves by subtree
    leaf_to_root = {}
    for leaf in leaves:
        current = leaf
        visited = set()
        while current and current in entries and current not in visited:
            visited.add(current)
            parent = entries[current]["parentUuid"]
            if not parent or parent not in entries:
                leaf_to_root[leaf] = current
                break
            current = parent

    # Group leaves by root
    root_to_leaves = defaultdict(list)
    for leaf, root in leaf_to_root.items():
        root_to_leaves[root].append(leaf)

    # For each root (in chronological order), find its active branch
    sorted_roots = sorted(root_to_leaves.keys(), key=lambda r: entries[r]["line"])

    all_messages = []
    overall_active_leaf = None
    branch_timestamps = []

    for root in sorted_roots:
        root_leaves = root_to_leaves[root]
        active_leaf = max(root_leaves, key=lambda l: entries[l]["line"])

        # Trace from active leaf to root
        path_uuids = []
        current = active_leaf
        visited = set()
        while current and current in entries and current not in visited:
            visited.add(current)
            path_uuids.append(current)
            current = entries[current]["parentUuid"]
        path_uuids.reverse()

        # Extract text messages and timestamps on this branch
        for uuid in path_uuids:
            td = entries[uuid]["text_data"]
            if td:
                all_messages.append(td)
            ts = entries[uuid]["timestamp"]
            if ts:
                branch_timestamps.append(ts)

        overall_active_leaf = active_leaf

    first_ts = branch_timestamps[0] if branch_timestamps else None
    last_ts = branch_timestamps[-1] if branch_timestamps else None

    return all_messages, file_size, overall_active_leaf, first_ts, last_ts, orphan_parents


def format_messages(messages):
    """Format messages as grep-friendly markdown."""
    lines = []
    for msg in messages:
        time_part = f" [{msg['time']}]" if msg["time"] else ""
        lines.append(f"## {msg['role']}{time_part}")
        lines.append("")
        lines.append(msg["text"])
        lines.append("")
    return "\n".join(lines)


def get_first_human_message(jsonl_path):
    """Extract first human message as session summary (for index)."""
    with open(jsonl_path, "r") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("type") != "user":
                    continue
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    first_line = content.strip().split("\n")[0][:100]
                    if first_line:
                        return first_line
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                return text.split("\n")[0][:100]
            except json.JSONDecodeError:
                continue
    return None


def detect_chains(state_dir, transcript_dir):
    """Detect session chains via cross-session parentUuid references.

    When `claude -c` continues a session, it creates entries with parentUuid
    referencing messages from the previous session. These orphan parentUuids
    (not found within the same session) are the cross-session links.

    Returns {session_id: continues_session_id} for sessions that continue another.
    """
    # Collect orphan_parents from state files
    session_orphans = {}
    for state_file in state_dir.glob("*.json"):
        try:
            state = json.loads(state_file.read_text())
            sid = state_file.stem
            orphans = state.get("orphan_parents", [])
            if orphans:
                session_orphans[sid] = orphans
        except (json.JSONDecodeError, IOError):
            continue

    if not session_orphans:
        return {}

    # Collect all target orphan uuids we need to find
    all_orphans = {}  # {orphan_uuid: session_that_needs_it}
    for sid, orphans in session_orphans.items():
        for orphan in orphans:
            all_orphans[orphan] = sid

    # Scan JSONL files to find which session contains each orphan uuid
    continues = {}
    remaining = set(all_orphans.keys())

    for jsonl_file in sorted(transcript_dir.glob("*.jsonl")):
        if not remaining:
            break
        if jsonl_file.stat().st_size == 0:
            continue
        source_sid = jsonl_file.stem

        with open(jsonl_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    uuid = entry.get("uuid", "")
                    if uuid in remaining:
                        child_sid = all_orphans[uuid]
                        if child_sid != source_sid:
                            continues[child_sid] = source_sid
                        remaining.discard(uuid)
                except json.JSONDecodeError:
                    continue

    return continues


def build_chains(continues):
    """Build ordered chains from the continues map.

    Returns list of chains, each chain is a list of session_ids in order.
    Standalone sessions (not in any chain) are returned as single-element chains.
    """
    # Find chain heads (sessions that continue nothing or whose parent isn't continued)
    all_sessions = set(continues.keys()) | set(continues.values())
    continued_by = {}  # reverse map: prev_sid → next_sid
    for sid, prev_sid in continues.items():
        continued_by[prev_sid] = sid

    # Find heads: sessions that are not a continuation of anything
    heads = [sid for sid in all_sessions if sid not in continues]

    chains = []
    seen = set()
    for head in sorted(heads):
        chain = [head]
        seen.add(head)
        current = head
        while current in continued_by:
            current = continued_by[current]
            chain.append(current)
            seen.add(current)
        chains.append(chain)

    return chains


def generate_session_files(chains_map, parts_dir, sessions_dir, state_dir):
    """Build searchable sessions/ from .parts/ files and chain info.

    - Chains: concatenated into sessions/chain-<head-id>.md
    - Standalone: copied as sessions/<session-id>.md
    """
    chains = build_chains(chains_map)

    # Sessions in multi-session chains
    chained_sids = set()
    for chain in chains:
        if len(chain) > 1:
            for sid in chain:
                chained_sids.add(sid)

    # All sessions with parts
    all_sids = {f.stem for f in parts_dir.glob("*.md")}

    # Clean sessions/ — remove stale files
    for existing in sessions_dir.glob("*.md"):
        existing.unlink()

    # Write chain files
    for chain in chains:
        if len(chain) < 2:
            continue

        head_sid = chain[0]
        chain_file = sessions_dir / f"chain-{head_sid[:8]}.md"

        # Get chain metadata from state
        total_messages = 0
        started = ""
        for sid in chain:
            state_file = state_dir / f"{sid}.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    first_ts = state.get("first_ts", "")
                    if first_ts and not started:
                        try:
                            dt = datetime.fromisoformat(
                                first_ts.replace("Z", "+00:00")
                            )
                            started = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, AttributeError):
                            started = first_ts[:19]
                except (json.JSONDecodeError, IOError):
                    pass

        with open(chain_file, "w") as f:
            f.write(f"---\nchain: {json.dumps(chain)}\n")
            f.write(f"sessions: {len(chain)}\n")
            if started:
                f.write(f"started: {started}\n")
            f.write("---\n\n")

            for i, sid in enumerate(chain):
                part_file = parts_dir / f"{sid}.md"
                if not part_file.exists():
                    continue

                # Session header
                session_date = ""
                state_file = state_dir / f"{sid}.json"
                if state_file.exists():
                    try:
                        state = json.loads(state_file.read_text())
                        first_ts = state.get("first_ts", "")
                        if first_ts:
                            try:
                                dt = datetime.fromisoformat(
                                    first_ts.replace("Z", "+00:00")
                                )
                                session_date = dt.strftime("%Y-%m-%d %H:%M")
                            except (ValueError, AttributeError):
                                session_date = first_ts[:16]
                    except (json.JSONDecodeError, IOError):
                        pass

                if i > 0:
                    f.write("\n---\n\n")
                f.write(f"# Session {sid[:8]} ({session_date})\n\n")
                f.write(part_file.read_text())

    # Write standalone files
    for sid in all_sids:
        if sid in chained_sids:
            continue
        part_file = parts_dir / f"{sid}.md"
        session_file = sessions_dir / f"{sid}.md"

        # Add frontmatter
        session_date = ""
        state_file = state_dir / f"{sid}.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                first_ts = state.get("first_ts", "")
                if first_ts:
                    try:
                        dt = datetime.fromisoformat(
                            first_ts.replace("Z", "+00:00")
                        )
                        session_date = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, AttributeError):
                        session_date = first_ts[:19]
            except (json.JSONDecodeError, IOError):
                pass

        with open(session_file, "w") as f:
            f.write(f"---\nsession_id: {sid}\n")
            if session_date:
                f.write(f"started: {session_date}\n")
            f.write("---\n\n")
            f.write(part_file.read_text())


def update_index(index_path, state_dir, session_summaries, transcript_dir):
    """Rewrite index.md with chain-grouped session entries.

    session_summaries: {session_id: (date_str, summary)}
    """
    chains_map = detect_chains(state_dir, transcript_dir)
    chains = build_chains(chains_map)

    # Sessions in chains
    chained_sids = set()
    for chain in chains:
        if len(chain) > 1:
            for sid in chain:
                chained_sids.add(sid)

    # Standalone sessions (not part of any multi-session chain)
    standalone = [
        sid for sid in session_summaries if sid not in chained_sids
    ]

    with open(index_path, "w") as f:
        f.write("# Conversation Index\n\n")

        # Write chains first
        for chain in chains:
            if len(chain) < 2:
                continue
            first_sid = chain[0]
            first_summary = session_summaries.get(first_sid)
            if first_summary:
                f.write(f"## Chain: {first_summary[1][:60]}\n")
            else:
                f.write(f"## Chain: {first_sid[:8]}\n")

            for i, sid in enumerate(chain):
                entry = session_summaries.get(sid)
                if not entry:
                    continue
                date_str, summary = entry
                if i == 0:
                    f.write(f"- **{sid}** ({date_str}) — {summary}\n")
                else:
                    f.write(
                        f"- **{sid}** ({date_str}) → continues {chain[i-1][:8]}\n"
                    )
            f.write("\n")

        # Write standalone sessions
        if standalone:
            if chained_sids:
                f.write("## Standalone\n")
            for sid in sorted(standalone):
                entry = session_summaries.get(sid)
                if entry:
                    date_str, summary = entry
                    f.write(f"- **{sid}** ({date_str}) — {summary}\n")


def git_commit(conv_dir, session_id, summary):
    """Commit changes in the conversations repo."""
    try:
        short_id = session_id[:8]
        msg = f"Update {short_id}: {summary[:60]}"
        subprocess.run(
            ["git", "add", "-A"], cwd=conv_dir, capture_output=True, timeout=5
        )
        # Check if there's anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=conv_dir,
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:  # There are staged changes
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=conv_dir,
                capture_output=True,
                timeout=5,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Don't break the hook if git fails


def sync_session(transcript_path, session_id, parts_dir, state_dir):
    """Process a single session JSONL into a part markdown file.

    Writes to .parts/<session_id>.md (internal). The searchable sessions/
    directory is built later by generate_session_files().

    Returns True if the session was updated, False if skipped.
    """
    # Check state: skip if file hasn't changed
    state_file = state_dir / f"{session_id}.json"
    prev_state = {}
    if state_file.exists():
        try:
            prev_state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass

    current_file_size = transcript_path.stat().st_size
    if prev_state.get("file_size") == current_file_size:
        return False

    # Extract active branch
    messages, file_size, active_leaf, first_ts, last_ts, orphan_parents = (
        extract_active_branch(transcript_path)
    )

    if not messages:
        state_file.write_text(
            json.dumps({"file_size": file_size, "leaf_uuid": None})
        )
        return False

    # Write individual part .md file
    part_file = parts_dir / f"{session_id}.md"
    with open(part_file, "w") as f:
        f.write(format_messages(messages))

    # Update state (include orphan_parents for chain detection)
    state_file.write_text(
        json.dumps({
            "file_size": file_size,
            "leaf_uuid": active_leaf,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "orphan_parents": orphan_parents,
        })
    )

    return True


def collect_summaries(state_dir, transcript_dir):
    """Collect session summaries for index rebuild.

    Returns {session_id: (date_str, summary)}.
    """
    summaries = {}
    for state_file in state_dir.glob("*.json"):
        sid = state_file.stem
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            continue
        # Skip sessions with no extracted messages
        if not state.get("leaf_uuid"):
            continue
        # Get date from first_ts
        first_ts = state.get("first_ts", "")
        date_str = ""
        if first_ts:
            try:
                dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, AttributeError):
                date_str = first_ts[:16]
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Get summary from JSONL
        jsonl_path = transcript_dir / f"{sid}.jsonl"
        summary = None
        if jsonl_path.exists():
            summary = get_first_human_message(jsonl_path)
        if not summary:
            summary = "[no summary]"
        summaries[sid] = (date_str, summary)
    return summaries


def rebuild_index(state_dir, index_path, transcript_dir, parts_dir, sessions_dir):
    """Rebuild index.md, detect chains, and generate searchable session files."""
    summaries = collect_summaries(state_dir, transcript_dir)
    if not summaries:
        return

    chains_map = detect_chains(state_dir, transcript_dir)
    generate_session_files(chains_map, parts_dir, sessions_dir, state_dir)
    update_index(index_path, state_dir, summaries, transcript_dir)


def backfill_sessions(
    transcript_path, parts_dir, sessions_dir, state_dir, index_path, conv_dir
):
    """Process all existing session JSONLs in the same project directory.

    Called on first run in a project to capture historical sessions.
    """
    project_jsonl_dir = transcript_path.parent
    count = 0
    for jsonl_file in sorted(project_jsonl_dir.glob("*.jsonl")):
        if jsonl_file.name.startswith("agent-"):
            continue
        if jsonl_file.stat().st_size == 0:
            continue
        sid = jsonl_file.stem
        if sync_session(jsonl_file, sid, parts_dir, state_dir):
            count += 1

    # Rebuild index with chain detection after all sessions are synced
    if count > 0:
        rebuild_index(
            state_dir, index_path, transcript_path.parent, parts_dir, sessions_dir
        )
        git_commit(conv_dir, "backfill", f"Backfill {count} historical sessions")


def main():
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # Determine project root
    project_root = get_project_root(hook_input)
    conv_dir = project_root / ".conversations"
    sessions_dir = conv_dir / "sessions"
    parts_dir = conv_dir / ".parts"
    state_dir = conv_dir / ".state"
    index_path = conv_dir / "index.md"

    # Find transcript
    transcript_path = find_transcript_path(hook_input)
    if not transcript_path:
        print(json.dumps({"suppressOutput": True}))
        return

    session_id = hook_input.get("session_id", transcript_path.stem)

    # Auto-setup on first run in this project
    first_run = not (conv_dir / ".git").exists()
    ensure_project_setup(project_root)

    # On first run, backfill all existing sessions from this project
    if first_run:
        backfill_sessions(
            transcript_path, parts_dir, sessions_dir, state_dir, index_path, conv_dir
        )

    # Process the current session
    if sync_session(transcript_path, session_id, parts_dir, state_dir):
        rebuild_index(
            state_dir, index_path, transcript_path.parent, parts_dir, sessions_dir
        )
        summary = get_first_human_message(transcript_path)
        if summary:
            git_commit(conv_dir, session_id, summary)

    # Suppress output — don't inject sync info into Claude's context
    print(json.dumps({"suppressOutput": True}))


if __name__ == "__main__":
    main()
