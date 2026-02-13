# cc-conversation

Conversation history tracking for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Survives compaction, supports search, fork, and revert.

## The problem

When Claude Code compacts a conversation, earlier details are summarized and the originals are lost. There's no way to recover what was discussed, no way to fork a conversation at a decision point, and no way to revert to an earlier state.

## What this does

A Claude Code hook that automatically extracts human/assistant text from session transcripts into grep-friendly markdown files, stored in a per-project `.conversations/` git repo.

- **Survives compaction** — the full conversation history is preserved across compaction boundaries
- **Chain-aware** — detects linked sessions (`claude -c`) and merges them into a single searchable file
- **Searchable** — Claude can grep past sessions when it needs to recall context
- **Git-versioned** — each sync creates a commit; fork, revert, or diff conversation history like code
- **Tree-aware** — follows only the active branch of the conversation, correctly handling rewinds
- **Noise-filtered** — auto-generated compaction summaries are excluded from output

## Install (one command)

```bash
curl -sSL https://raw.githubusercontent.com/daiduongtruong/cc-conversation/master/install-global.sh | bash
```

This installs globally for all projects. On first Claude response in any project, it auto-creates `.conversations/` and backfills all existing sessions.

### Update

Re-run the same install command to update to the latest version:

```bash
curl -sSL https://raw.githubusercontent.com/daiduongtruong/cc-conversation/master/install-global.sh | bash
```

### Per-project install (alternative)

```bash
git clone https://github.com/daiduongtruong/cc-conversation.git
bash cc-conversation/install.sh /path/to/your/project
```

### Uninstall

```bash
curl -sSL https://raw.githubusercontent.com/daiduongtruong/cc-conversation/master/install-global.sh | bash -s -- --uninstall
```

## How it works

Three Claude Code hooks are configured:

| Hook | When | What |
|------|------|------|
| **Stop** | After each Claude response | Syncs conversation to `.conversations/` |
| **PreCompact** | Before compaction | Same sync (captures conversation before it's summarized) |
| **SessionStart** | New session starts | Shows conversation index to Claude |

The sync script:

1. Reads the session JSONL transcript
2. Builds a UUID tree from `parentUuid` links
3. Finds the active leaf (most recently written, non-sidechain)
4. Traces from each root's active leaf back to root, crossing compaction boundaries
5. Extracts only human/assistant text (no tool use, no tool results, no compaction summaries)
6. Detects session chains via cross-session `parentUuid` references
7. Merges chained sessions into a single file; writes standalone sessions individually
8. Writes grep-friendly markdown with timestamped `## User` / `## Assistant` headers
9. Commits to the `.conversations/` git repo

## What gets created

```
your-project/
└── .conversations/              # separate git repo
    ├── index.md                 # session index (chains grouped)
    ├── sessions/
    │   ├── chain-<head-id>.md   # chained sessions merged into one file
    │   └── <session-id>.md      # standalone sessions
    ├── .parts/                  # internal (gitignored)
    └── .state/                  # internal (gitignored)
```

Add `.conversations/` to your project's `.gitignore` — it's a local tool, not project source.

## Session chains

When you continue a conversation with `claude -c`, Claude Code creates a new session that references the previous one. This tool detects those links and merges the entire chain into a single `chain-<id>.md` file, so Claude can search across the full conversation without jumping between files.

Chain files include frontmatter with the session list and boundaries marked by `# Session <id>` headers.

## Conversation recovery

After compaction, Claude automatically knows how to search history (via `~/.claude/CLAUDE.md` instructions). It can:

```
Grep pattern="authentication" path=".conversations/sessions/"
```

To read a specific section, use `Read` with offset/limit on the matched file.

## Conversation versioning

Each sync is a git commit in `.conversations/`. This supports:

- **Revert**: `git log --oneline` then `git checkout <commit>`
- **Fork**: `git checkout -b <branch>` from any checkpoint
- **Compare**: `git diff <branch1> <branch2>`

## Performance

| File size | Messages | Time |
|-----------|----------|------|
| 2.4 MB | 132 | 0.011s |
| 16 MB | 63 | 0.044s |
| 81 MB | 1039 | 0.24s |

## Requirements

- Claude Code
- Python 3.6+
- git
