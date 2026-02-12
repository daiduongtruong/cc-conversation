#!/usr/bin/env bash
set -euo pipefail

# install.sh — Install conversation history tracking into a Claude Code project.
#
# Usage:
#   cd /path/to/your/project
#   bash /path/to/ai-conversation-management/install.sh
#
# Or with an explicit target:
#   bash install.sh /path/to/your/project
#
# What it does:
#   1. Copies sync-conversation.py to .claude/hooks/
#   2. Merges hook config into .claude/settings.local.json (preserves existing settings)
#   3. Appends conversation recovery instructions to CLAUDE.md (preserves existing content)
#   4. Initializes .conversations/ as a separate git repo
#
# Safe to run multiple times (idempotent).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="${1:-$(pwd)}"

echo "Installing conversation history tracking into: $TARGET_DIR"
echo

# --- 1. Copy the hook script ---
echo "[1/4] Installing sync-conversation.py..."
mkdir -p "$TARGET_DIR/.claude/hooks"
cp "$SCRIPT_DIR/sync-conversation.py" "$TARGET_DIR/.claude/hooks/sync-conversation.py"
chmod +x "$TARGET_DIR/.claude/hooks/sync-conversation.py"
echo "  -> .claude/hooks/sync-conversation.py"

# --- 2. Merge hooks into settings.local.json ---
echo "[2/4] Configuring hooks in settings.local.json..."

SETTINGS_FILE="$TARGET_DIR/.claude/settings.local.json"

python3 -c "
import json, sys

settings_file = '$SETTINGS_FILE'

# Load existing settings or start fresh
try:
    with open(settings_file) as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

# Ensure hooks structure exists
if 'hooks' not in settings:
    settings['hooks'] = {}

hooks = settings['hooks']

# Hook definitions to install
sync_hook = {'type': 'command', 'command': 'python3 .claude/hooks/sync-conversation.py'}
session_hook = {
    'type': 'command',
    'command': \"if [ -f .conversations/index.md ]; then echo '## Conversation History Available'; echo ''; cat .conversations/index.md; echo ''; echo 'Search .conversations/sessions/ when you need to recall prior decisions.'; fi\"
}

def has_sync_hook(hook_list):
    \"\"\"Check if sync-conversation.py is already configured.\"\"\"
    for group in hook_list:
        for h in group.get('hooks', []):
            if 'sync-conversation.py' in h.get('command', ''):
                return True
    return False

def has_session_hook(hook_list):
    \"\"\"Check if session start index hook is already configured.\"\"\"
    for group in hook_list:
        for h in group.get('hooks', []):
            if 'conversations/index.md' in h.get('command', ''):
                return True
    return False

changed = False

# Add Stop hook
if 'Stop' not in hooks:
    hooks['Stop'] = []
if not has_sync_hook(hooks['Stop']):
    hooks['Stop'].append({'hooks': [sync_hook]})
    changed = True

# Add PreCompact hook
if 'PreCompact' not in hooks:
    hooks['PreCompact'] = []
if not has_sync_hook(hooks['PreCompact']):
    hooks['PreCompact'].append({'hooks': [sync_hook]})
    changed = True

# Add SessionStart hook
if 'SessionStart' not in hooks:
    hooks['SessionStart'] = []
if not has_session_hook(hooks['SessionStart']):
    hooks['SessionStart'].append({'hooks': [session_hook]})
    changed = True

with open(settings_file, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

if changed:
    print('  -> Hooks added to .claude/settings.local.json')
else:
    print('  -> Hooks already configured, no changes needed')
"

# --- 3. Append to CLAUDE.md ---
echo "[3/4] Adding conversation recovery instructions to CLAUDE.md..."

CLAUDE_MD="$TARGET_DIR/CLAUDE.md"
MARKER="# Conversation History Recovery"

if [ -f "$CLAUDE_MD" ] && grep -qF "$MARKER" "$CLAUDE_MD"; then
    echo "  -> CLAUDE.md already contains conversation recovery instructions, skipping"
else
    # Append with a separator if file already has content
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

# --- 4. Initialize .conversations/ git repo ---
echo "[4/4] Initializing .conversations/ repository..."

CONV_DIR="$TARGET_DIR/.conversations"
mkdir -p "$CONV_DIR/sessions" "$CONV_DIR/.state"

if [ -d "$CONV_DIR/.git" ]; then
    echo "  -> .conversations/ already initialized, skipping"
else
    cd "$CONV_DIR"
    git init -q
    cat > index.md << 'EOF'
# Conversation Index

EOF
    git add -A
    git commit -q -m "Init conversation history"
    echo "  -> Initialized .conversations/ as git repo"
fi

# --- Done ---
echo
echo "Done! Conversation history tracking is now active."
echo
echo "What happens next:"
echo "  - Every Claude response syncs the conversation to .conversations/sessions/"
echo "  - Each session start shows the conversation index to Claude"
echo "  - Claude can grep past sessions when it needs to recall context"
echo "  - Each sync creates a git commit for fork/revert support"
echo
echo "Optional: add .conversations/ to your code repo's .gitignore:"
echo "  echo '.conversations/' >> $TARGET_DIR/.gitignore"
