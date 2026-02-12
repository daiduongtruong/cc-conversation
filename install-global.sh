#!/usr/bin/env bash
set -euo pipefail

# install-global.sh — Install conversation history tracking globally for all Claude Code projects.
#
# Usage:
#   bash /path/to/ai-conversation-management/install-global.sh
#
# What it does:
#   1. Copies sync-conversation.py to ~/.claude/hooks/
#   2. Merges hook config into ~/.claude/settings.json (preserves existing settings)
#   3. Appends conversation recovery instructions to ~/.claude/CLAUDE.md
#
# After installation, every Claude Code session in ANY project will:
#   - Auto-create .conversations/ on first use
#   - Sync conversation text after each response
#   - Maintain a git-versioned conversation history
#   - Claude knows how to search history from the first message (via global CLAUDE.md)
#
# Safe to run multiple times (idempotent).
# To uninstall: bash install-global.sh --uninstall

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_HOME="$HOME/.claude"
HOOKS_DIR="$CLAUDE_HOME/hooks"
SETTINGS_FILE="$CLAUDE_HOME/settings.json"

# --- Uninstall ---
if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling global conversation history tracking..."

    if [ -f "$HOOKS_DIR/sync-conversation.py" ]; then
        rm "$HOOKS_DIR/sync-conversation.py"
        echo "  Removed $HOOKS_DIR/sync-conversation.py"
    fi

    if [ -f "$SETTINGS_FILE" ]; then
        python3 -c "
import json

settings_file = '$SETTINGS_FILE'
with open(settings_file) as f:
    settings = json.load(f)

hooks = settings.get('hooks', {})
changed = False

for event in ['Stop', 'PreCompact', 'SessionStart']:
    if event in hooks:
        original = len(hooks[event])
        hooks[event] = [
            g for g in hooks[event]
            if not any('sync-conversation.py' in h.get('command', '') or
                       'conversations/index.md' in h.get('command', '')
                       for h in g.get('hooks', []))
        ]
        if len(hooks[event]) != original:
            changed = True
        if not hooks[event]:
            del hooks[event]

if not hooks:
    settings.pop('hooks', None)

with open(settings_file, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

if changed:
    print('  Removed hooks from', settings_file)
else:
    print('  No hooks to remove in', settings_file)
"
    fi

    echo
    echo "Uninstalled. Per-project .conversations/ directories are preserved."
    exit 0
fi

# --- Install ---
echo "Installing global conversation history tracking..."
echo "  Hook script:  $HOOKS_DIR/sync-conversation.py"
echo "  Settings:     $SETTINGS_FILE"
echo

# 1. Copy hook script
echo "[1/3] Installing sync-conversation.py to ~/.claude/hooks/..."
mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/sync-conversation.py" "$HOOKS_DIR/sync-conversation.py"
chmod +x "$HOOKS_DIR/sync-conversation.py"
echo "  -> $HOOKS_DIR/sync-conversation.py"

# 2. Merge hooks into settings.json
echo "[2/3] Configuring hooks in ~/.claude/settings.json..."

python3 -c "
import json, os

settings_file = '$SETTINGS_FILE'

# Load existing settings or start fresh
try:
    with open(settings_file) as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

if 'hooks' not in settings:
    settings['hooks'] = {}

hooks = settings['hooks']

sync_cmd = 'python3 \"\$HOME/.claude/hooks/sync-conversation.py\"'
sync_hook = {'type': 'command', 'command': sync_cmd}

index_cmd = \"if [ -f .conversations/index.md ]; then echo '## Conversation History Available'; echo ''; cat .conversations/index.md; echo ''; echo 'Search .conversations/sessions/ when you need to recall prior decisions.'; fi\"
session_hook = {'type': 'command', 'command': index_cmd}

def has_hook(hook_list, needle):
    for group in hook_list:
        for h in group.get('hooks', []):
            if needle in h.get('command', ''):
                return True
    return False

changed = False

for event in ['Stop', 'PreCompact']:
    if event not in hooks:
        hooks[event] = []
    if not has_hook(hooks[event], 'sync-conversation.py'):
        hooks[event].append({'hooks': [sync_hook]})
        changed = True

if 'SessionStart' not in hooks:
    hooks['SessionStart'] = []
if not has_hook(hooks['SessionStart'], 'conversations/index.md'):
    hooks['SessionStart'].append({'hooks': [session_hook]})
    changed = True

with open(settings_file, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

if changed:
    print('  -> Hooks added to ~/.claude/settings.json')
else:
    print('  -> Hooks already configured, no changes needed')
"

# 3. Append instructions to global CLAUDE.md
echo "[3/3] Adding conversation recovery instructions to ~/.claude/CLAUDE.md..."

CLAUDE_MD="$CLAUDE_HOME/CLAUDE.md"
MARKER="# Conversation History Recovery"

if [ -f "$CLAUDE_MD" ] && grep -qF "$MARKER" "$CLAUDE_MD"; then
    echo "  -> Already contains conversation recovery instructions, skipping"
else
    if [ -f "$CLAUDE_MD" ] && [ -s "$CLAUDE_MD" ]; then
        echo "" >> "$CLAUDE_MD"
        echo "---" >> "$CLAUDE_MD"
        echo "" >> "$CLAUDE_MD"
    fi
    cat >> "$CLAUDE_MD" << 'CLAUDE_CONTENT'
# Conversation History Recovery

Full conversation transcripts (human/assistant text only, no tool output) are
stored in `.conversations/sessions/` as grep-friendly markdown files.
A session index is at `.conversations/index.md`.

## When to search conversation history

- After compaction, if you are uncertain about a prior decision or context
- When the user references something discussed earlier that you cannot find in current context
- When starting work that builds on previous sessions
- When the user explicitly asks you to recall or check history

## How to search

1. Use Grep to search across all sessions:
   `Grep pattern=".." path=".conversations/sessions/"`

2. To read a specific section, use Read with offset/limit — NEVER read an entire session file.

3. Each session file has timestamped `## User` and `## Assistant` headers.
   Search for keywords near these headers to find the relevant exchange.

## Conversation versioning

The `.conversations/` directory is its own git repo. Each Claude response
creates a commit. This supports:

- **Revert**: `git log --oneline` in `.conversations/` to see checkpoints,
  then `git checkout <commit>` to revert to any conversation state.
- **Fork**: `git checkout -b <branch-name>` from any checkpoint to explore
  an alternative conversation path.
- **Compare**: `git diff <branch1> <branch2>` to see how conversation
  branches diverged.

## Custom resume (alternative to `claude --resume`)

To continue from a past session without loading the full history:
1. Start a fresh Claude session in this project
2. Claude will see the index via SessionStart hook
3. Ask Claude to search the relevant session for context
4. Continue working with only the relevant context loaded
CLAUDE_CONTENT
    echo "  -> Appended to $CLAUDE_MD"
fi

echo
echo "Done! Global conversation history tracking is active."
echo
echo "How it works:"
echo "  - Claude knows how to search history from the first message (via ~/.claude/CLAUDE.md)"
echo "  - On first Claude response in ANY project, auto-creates:"
echo "    .conversations/   (separate git repo for searchable transcripts)"
echo "  - Every subsequent response syncs incrementally"
echo "  - Each sync = a git commit (supports fork/revert)"
echo
echo "To uninstall:"
echo "  bash $SCRIPT_DIR/install-global.sh --uninstall"
