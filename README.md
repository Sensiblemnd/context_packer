# dgc — Context Packer for Claude Code

Pack your codebase into a single `CLAUDE_CONTEXT.md` so Claude Code spends tokens **solving problems, not exploring files**. Saves ~30–40% tokens per session.

No telemetry. No config files. Pure Python stdlib — optional deps add accuracy.

---

## How it works

1. Scans your project and scores every file by importance (git recency, commit churn, depth, name, prompt keywords)
2. Strips noise — JSDoc blocks, docstrings, import statements — before packing
3. Summarises config files (`package.json`, `tsconfig.json`) as compact one-liners
4. Writes `CLAUDE_CONTEXT.md` with a project summary, architecture map, file tree with token counts, and file contents
5. Optionally launches Claude Code with the context already loaded

Claude reads the file at session start instead of spending 5–10 turns exploring your codebase.

---

## Requirements

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
- Git (optional — improves file scoring significantly)

**Optional — install for best results:**

```bash
pip install tiktoken watchdog pathspec
```

| Package | Effect |
|---|---|
| `tiktoken` | Accurate token counting (falls back to `chars / 4`) |
| `watchdog` | Efficient file-system events in `--watch` mode (falls back to polling) |
| `pathspec` | Reads your `.gitignore` and `.dgcignore` (falls back to built-in skip list) |

---

## Install

**Windows:**
```powershell
python dgc.py
```

**Linux / macOS:**
```bash
chmod +x dgc.py
sudo cp dgc.py /usr/local/bin/dgc
dgc
```

---

## Usage

```bash
# Interactive — scans and asks what to do
dgc
python dgc.py

# Just create CLAUDE_CONTEXT.md
dgc --context-only

# Create context + launch Claude
dgc --launch

# Create context + launch Claude with a starting prompt
dgc --launch "fix the login bug"

# Only repack files changed since last run
dgc --refresh
dgc --refresh --launch

# Only pack a specific subfolder
dgc --focus src/reducers
dgc --focus src/api --launch "add rate limiting"

# Exclude patterns (repeatable, glob syntax)
dgc --exclude "**/*.test.ts" --exclude "DOC/" --exclude "**/*.spec.*"

# Balance folder representation (--diverse mode)
dgc --diverse

# Auto-regenerate context whenever files change
dgc --watch
dgc --watch --focus src/reducers
```

---

## Interactive mode

Running with no flags asks before writing anything:

```
[dgc] Scanning git-bloom...
[dgc] Using: tiktoken, pathspec, watchdog, .gitignore
[dgc] Git: 12 recently changed files prioritised
[dgc] Keywords: login, auth

What would you like to do?
  1) Just create the context file
  2) Create the context file + launch Claude Code
Enter 1 or 2:
```

---

## File scoring

Files are ranked — higher score = included first and within token budget:

| Signal | Score |
|---|---|
| Recently changed (`git log -20`) | +50 |
| Frequently changed (commit churn) | up to +30 |
| Name matches prompt keywords | +25 per keyword |
| Important name (`index`, `app`, `config`, `routes`, `api`…) | +30 |
| Config / entry pattern in path | +15 |
| Shallow depth in project | up to +15 |
| Small file (< 5 KB) | +10 |
| Large file (> 50 KB) | −10 |

---

## Noise stripping

Removed from every file before packing:

| Language | Stripped |
|---|---|
| TypeScript / JavaScript | `import` statements, JSDoc `/** */` blocks |
| Python | `import`/`from` statements, docstrings (AST-based, regex fallback) |
| Go | `import` blocks (single and grouped) |
| Rust | `use` statements |
| Java / Kotlin | `import` statements |
| C# | `using` statements |
| All | Excess blank lines collapsed |

Config files are summarised rather than included in full:

- `package.json` → scripts, deps, devDeps (one-liner)
- `tsconfig.json` → target, jsx, strict, baseUrl (one-liner)
- `.eslintrc.json`, `postcss.config.js`, `.babelrc` → compact JSON, max 400 chars

---

## Refresh mode

```bash
dgc --refresh
```

Diffs the current file set against the last session using BLAKE2b hashes:

- **Modified** — file exists, hash changed
- **Added** — file is new since last run
- **Removed** — file was in last session, no longer on disk

Output:
```
[dgc] Refresh: 3 modified, 1 added, 2 removed
```

Removed files appear in `CLAUDE_CONTEXT.md` under `## Removed Files` so Claude knows what's gone.
Unchanged files are listed under `## Unchanged` and excluded from the packed content.

Session state is stored in `.dgc-session.json`.

---

## Watch mode

```bash
dgc --watch
```

Regenerates `CLAUDE_CONTEXT.md` automatically on file changes.

- With `watchdog`: event-driven + a 30-second periodic rescan to catch new and deleted files that events can miss
- Without `watchdog`: full rescan every 2 seconds (catches all change types including new/deleted)

```
[dgc] Watch mode active. Ctrl+C to stop.
[dgc] Using watchdog (+ 30s rescan for new/deleted files).
[dgc] Regenerated — 38 files, ~9,200 tokens (14:23:01)
[dgc] Regenerated — 39 files, ~9,350 tokens (14:25:44)
```

---

## Diverse mode

```bash
dgc --diverse
```

Caps files per directory at 5 before lower-scoring directories get slots. Useful for large monorepos where one hot folder would otherwise dominate the context.

Not recommended for typical projects — the scoring system already handles relevance well.

---

## Persistent notes

Create `CLAUDE_NOTES.md` in your project root. Injected into every context automatically.

```markdown
# CLAUDE_NOTES.md

## Architecture decisions
- All state lives in useReducer + Context — no external state lib
- Reducer handlers are split into individual files under src/reducers/
- pnpm only — npm is blocked via engine config

## Known issues
- handle-tick.ts: race condition when legacy plant dies on same tick as standup
- PR queue desyncs after sprint reset — see issue #42
```

---

## Output files

| File | Description |
|---|---|
| `CLAUDE_CONTEXT.md` | Generated — read by Claude at session start |
| `CLAUDE_NOTES.md` | Your persistent notes — edit manually |
| `.dgc-session.json` | Session cache for `--refresh` (BLAKE2b hashes) |

Add generated files to `.gitignore`:

```bash
echo "CLAUDE_CONTEXT.md" >> .gitignore
echo ".dgc-session.json" >> .gitignore
```

You can also create `.dgcignore` with additional patterns dgc should ignore (same gitignore syntax).

---

## vs RepoMix

dgc is intentionally simpler and Claude Code-focused. RepoMix has more features if you need:

- Tree-sitter AST compression (~70% token reduction)
- Secretlint content scanning
- Remote GitHub repo packing by URL
- MCP server mode
- Per-folder token breakdowns
- Output splitting for very large repos

dgc advantages: no Node.js required, no install step, works anywhere Python 3.10+ runs, integrates directly with Claude Code launch.

---

## License

MIT
