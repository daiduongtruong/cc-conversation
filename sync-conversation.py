#!/usr/bin/env python3
"""
sync-conversation.py — Maintain search-friendly conversation transcripts.

Triggered by Claude Code hooks (Stop, PreCompact).
Reads the session JSONL, extracts human/assistant text (no tool blocks),
writes grep-friendly markdown to .conversations/sessions/<session-id>.md.

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
    state_dir = conv_dir / ".state"
    index_path = conv_dir / "index.md"

    # Create directory structure
    sessions_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Add .state/ to .gitignore (implementation detail, not conversation content)
    gitignore = conv_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".state/\n")
    elif ".state/" not in gitignore.read_text():
        with open(gitignore, "a") as f:
            f.write(".state/\n")

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

    Returns (messages, file_size, active_leaf_uuid).
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
                    timestamp = entry.get("timestamp", "")
                    time_str = ""
                    if timestamp:
                        try:
                            dt = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, AttributeError):
                            time_str = timestamp[:19]

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
            }

            if parent_uuid:
                children[parent_uuid].append(uuid)

    if not entries:
        return [], file_size, None

    # Find non-sidechain leaves
    leaves = [
        u
        for u in entries
        if u not in children and not entries[u]["isSidechain"]
    ]
    if not leaves:
        leaves = [u for u in entries if u not in children]
    if not leaves:
        return [], file_size, None

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

        # Extract text messages on this branch
        for uuid in path_uuids:
            td = entries[uuid]["text_data"]
            if td:
                all_messages.append(td)

        overall_active_leaf = active_leaf

    return all_messages, file_size, overall_active_leaf


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


def update_index(index_path, session_id, summary):
    """Update index.md with session entry."""
    entries = {}

    if index_path.exists():
        with open(index_path, "r") as f:
            for line in f:
                line = line.rstrip()
                if line.startswith("- **"):
                    try:
                        sid = line.split("**")[1]
                        entries[sid] = line
                    except IndexError:
                        pass

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    entries[session_id] = f"- **{session_id}** ({date_str}) — {summary}"

    with open(index_path, "w") as f:
        f.write("# Conversation Index\n\n")
        for entry in entries.values():
            f.write(entry + "\n")


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


def sync_session(transcript_path, session_id, sessions_dir, state_dir, index_path):
    """Process a single session JSONL into a markdown file.

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
    messages, file_size, active_leaf = extract_active_branch(transcript_path)

    if not messages:
        state_file.write_text(
            json.dumps({"file_size": file_size, "leaf_uuid": None})
        )
        return False

    # Always write full .md file (active branch may have changed on rewind)
    session_file = sessions_dir / f"{session_id}.md"
    with open(session_file, "w") as f:
        f.write(f"---\nsession_id: {session_id}\n")
        f.write(f"started: {messages[0]['time'] or datetime.now().isoformat()}\n")
        f.write(f"messages: {len(messages)}\n---\n\n")
        f.write(format_messages(messages))

    # Update state
    state_file.write_text(
        json.dumps({"file_size": file_size, "leaf_uuid": active_leaf})
    )

    # Update index
    summary = get_first_human_message(transcript_path)
    if summary:
        update_index(index_path, session_id, summary)

    return True


def backfill_sessions(transcript_path, sessions_dir, state_dir, index_path, conv_dir):
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
        if sync_session(jsonl_file, sid, sessions_dir, state_dir, index_path):
            count += 1

    if count > 0:
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
            transcript_path, sessions_dir, state_dir, index_path, conv_dir
        )

    # Process the current session
    if sync_session(
        transcript_path, session_id, sessions_dir, state_dir, index_path
    ):
        summary = get_first_human_message(transcript_path)
        if summary:
            git_commit(conv_dir, session_id, summary)

    # Suppress output — don't inject sync info into Claude's context
    print(json.dumps({"suppressOutput": True}))


if __name__ == "__main__":
    main()
