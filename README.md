# dgc — Context Packer for Claude Code

Pack your codebase into a single `CLAUDE_CONTEXT.md` so Claude Code spends tokens **solving problems, not exploring files**. Saves ~30–40% tokens per session.

No telemetry. No config files. Pure Python stdlib — optional deps add accuracy.

---

## How it works

1. Scans your project and scores every file by importance (git recency, commit churn, depth, name, prompt keywords)
2. Scans file content for accidentally committed secrets — warns and redacts before packing
3. Strips noise — JSDoc blocks, docstrings, import statements — before packing
4. Summarises config files (`package.json`, `tsconfig.json`) as compact one-liners
5. Writes `CLAUDE_CONTEXT.md` with a project summary, architecture map, file tree with per-file token counts, and file contents
6. Optionally launches Claude Code with the context already loaded

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

# Just create CLAUDE_CONTEXT.md
dgc --context-only

# Create context + launch Claude
dgc --launch

# Create context + launch Claude with a starting prompt
dgc --launch "fix the login bug"

# Preview what would be packed — no files written
dgc --dry-run

# Token breakdown by folder after packing
dgc --stats
dgc --context-only --stats

# Only repack files changed since last run
dgc --refresh
dgc --refresh --launch

# Only pack a specific subfolder
dgc --focus src/reducers
dgc --focus src/api --launch "add rate limiting"

# Exclude patterns (repeatable, glob syntax)
dgc --exclude "**/*.test.ts" --exclude "DOC/" --exclude "**/*.spec.*"

# Balance folder representation
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

What would you like to do?
  1) Just create the context file
  2) Create the context file + launch Claude Code
Enter 1 or 2:
```

---

## Secret scanning

Before packing any file, dgc scans its content for common credential patterns:

| Pattern | Example |
|---|---|
| OpenAI key | `sk-...` |
| GitHub token | `ghp_...` |
| AWS access key | `AKIA...` |
| Private key header | `-----BEGIN ... PRIVATE KEY-----` |
| Generic API key | `api_key = "..."` |
| Generic secret | `password = "..."` |
| DB connection string | `postgres://user:pass@...` |

When a match is found:
```
[dgc] ⚠ Possible secret in login.ts: GitHub token, Generic secret
```

The matched content is **redacted** in the packed output — Claude sees `[REDACTED:GitHub token]` instead of the actual value. The original file is never modified.

`.env` files and their variants (`.env.local`, `.env.production`, etc.) are excluded entirely and never packed.

---

## Dry run

Preview exactly what would be packed and how many tokens it would use — without writing anything:

```bash
dgc --dry-run
```

```
[dgc] Dry run — 38 files would be packed:
  src/reducers/game-reducer.ts  (~240 tokens)
  src/routes/index.tsx          (~180 tokens)
  src/lib/constants.ts          (~310 tokens)
  ...

[dgc] Estimated total: ~9,240 tokens
[dgc] No files written.
```

Useful for tuning `--focus` and `--exclude` before committing to a scan.

---

## Stats mode

After packing, print a token breakdown by folder:

```bash
dgc --stats
dgc --context-only --stats
```

```
[dgc] Token usage by folder:
  src/reducers                        3,240 tokens   18%  ██████
  src/components                      2,890 tokens   16%  █████
  src/lib                             1,200 tokens    7%  ██
  src/routes                            980 tokens    5%  █
  (root)                                420 tokens    2%  █
```

Helps you decide where to `--focus` or `--exclude` on the next run.

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
| Python | `import`/`from` statements, docstrings (AST-based, regex fallback on syntax error) |
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

```
[dgc] Refresh: 3 modified, 1 added, 2 removed
```

Removed files appear in `CLAUDE_CONTEXT.md` under `## Removed Files` so Claude knows what's gone. Unchanged files are listed under `## Unchanged` and excluded from packed content.

---

## Watch mode

```bash
dgc --watch
```

Regenerates `CLAUDE_CONTEXT.md` automatically on file changes.

- With `watchdog`: event-driven + 30-second periodic rescan to catch new and deleted files
- Without `watchdog`: full rescan every 2 seconds (catches all change types)

```
[dgc] Watch mode active. Ctrl+C to stop.
[dgc] Regenerated — 38 files, ~9,200 tokens (14:23:01)
[dgc] Regenerated — 39 files, ~9,350 tokens (14:25:44)
```

---

## Persistent notes

Create `CLAUDE_NOTES.md` in your project root. Injected into every context automatically.

```markdown
## Architecture decisions
- All state lives in useReducer + Context — no external state lib
- Reducer handlers are split into individual files under src/reducers/
- pnpm only — npm is blocked via engine config

## Known issues
- handle-tick.ts: race condition when legacy plant dies on same tick as standup
```

---

## Ignore files

| File | Purpose |
|---|---|
| `.gitignore` | Read automatically — all patterns respected |
| `.dgcignore` | dgc-specific excludes (same gitignore syntax) |

The context file itself (`CLAUDE_CONTEXT.md`) and session file (`.dgc-session.json`) are always excluded.

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

---

## vs RepoMix

dgc is intentionally simpler and Claude Code-focused. RepoMix has more features if you need:

- Tree-sitter AST compression (~70% token reduction)
- Remote GitHub repo packing by URL
- MCP server mode
- Output splitting for very large repos

dgc advantages: no Node.js required, no install step, works anywhere Python 3.10+ runs, secret scanning with redaction, integrates directly with Claude Code launch.

---

## License

MIT
