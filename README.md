# dgc — Context Packer for Claude Code

Pack your codebase into a single `CLAUDE_CONTEXT.md` so Claude Code spends tokens **solving problems, not exploring files**. Saves ~30–40% tokens per session.

No telemetry. No config files. No dependencies beyond Python stdlib.

---

## How it works

1. Scans your project and scores every file by importance (git recency, churn frequency, depth, name, prompt keywords)
2. Strips noise — JSDoc blocks, docstrings, import statements — before packing
3. Summarises config files (`package.json`, `tsconfig.json`) as compact one-liners
4. Writes a single `CLAUDE_CONTEXT.md` with a project summary, architecture inference, file tree, and file contents
5. Optionally launches Claude Code with the context already loaded

Claude reads the file at session start instead of spending 5–10 turns calling `read_file` to explore your codebase.

---

## Requirements

- Python 3.8+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
- Git (optional — improves file scoring)

**Optional — install for best results:**

```bash
pip install tiktoken watchdog
```

- `tiktoken` — accurate token counting (falls back to `chars / 4` without it)
- `watchdog` — efficient file-system events in `--watch` mode (falls back to polling)

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

# Just create CLAUDE_CONTEXT.md, don't launch Claude
dgc --context-only

# Create context + launch Claude
dgc --launch

# Create context + launch Claude with a starting prompt
dgc --launch "fix the login bug"

# Only repack files that changed since last run
dgc --refresh
dgc --refresh --launch

# Only pack a specific subfolder
dgc --focus src/reducers
dgc --focus src/api --launch "add rate limiting"

# Auto-regenerate context whenever files change
dgc --watch
dgc --watch --focus src/reducers
```

---

## Interactive mode

Running with no flags asks before writing anything:

```
[dgc] Scanning git-bloom...
[dgc] Git: 12 recently changed files prioritised
[dgc] Keywords: login, auth

What would you like to do?
  1) Just create the context file
  2) Create the context file + launch Claude Code
Enter 1 or 2:
```

---

## File scoring

Files are ranked — higher score means included first and more likely to stay within the token budget:

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

| Language | What's stripped |
|---|---|
| TypeScript / JavaScript | `import` statements, JSDoc `/** */` blocks |
| Python | `import` / `from` statements, triple-quoted docstrings (AST-based) |
| Go | `import` blocks (single and grouped) |
| Rust | `use` statements |
| Java / Kotlin | `import` statements |
| C# | `using` statements |
| All | Excess blank lines collapsed to max 2 |

Config files are summarised rather than included in full:

- `package.json` → scripts, deps, devDeps (one-liner)
- `tsconfig.json` → target, jsx, strict, baseUrl (one-liner)
- `.eslintrc.json`, `postcss.config.js`, `.babelrc` → compact JSON, max 400 chars

---

## Session memory & refresh mode

After each run, dgc saves `.dgc-session.json` with a hash of every packed file.

On the next run, `--refresh` compares current hashes against the session and only repacks files that changed — skipping unchanged files entirely and listing them in the context as "unchanged". Useful mid-session when you've only touched a few files.

---

## Watch mode

```bash
dgc --watch
```

Regenerates `CLAUDE_CONTEXT.md` automatically whenever files change. Uses `watchdog` for efficient file-system events if installed, otherwise polls every 2 seconds.

```
[dgc] Watch mode active. Ctrl+C to stop.
[dgc] Using watchdog for efficient file-system events.
[dgc] Regenerated — 38 files, ~9,200 tokens (14:23:01)
[dgc] Regenerated — 38 files, ~9,240 tokens (14:25:44)
```

---

## Persistent notes

Create `CLAUDE_NOTES.md` in your project root. It gets injected into every context file automatically — use it for anything you always want Claude to know about the project.

```markdown
# CLAUDE_NOTES.md

## Architecture decisions
- All state lives in useReducer + Context — no external state lib
- Reducer handlers are split into individual files under src/reducers/
- pnpm only — npm is blocked by engine config

## Known issues
- handle-tick.ts: race condition when legacy plant dies on same tick as standup
- PR queue sometimes desyncs after sprint reset
```

---

## Output files

| File | Description |
|---|---|
| `CLAUDE_CONTEXT.md` | Generated — read by Claude at session start |
| `CLAUDE_NOTES.md` | Yours to edit — injected into every context |
| `.dgc-session.json` | Session cache used by `--refresh` |

Add the generated files to `.gitignore`:

```bash
echo "CLAUDE_CONTEXT.md" >> .gitignore
echo ".dgc-session.json" >> .gitignore
```

---

## vs RepoMix

dgc is intentionally simpler and Claude Code-focused. RepoMix is the more fully-featured tool if you need:

- Tree-sitter AST compression (~70% token reduction)
- Secretlint secret scanning on file contents
- `.gitignore` / `.repomixignore` file respect
- Remote GitHub repo packing by URL
- MCP server mode
- Per-file token counts in the tree
- Output splitting for very large repos

dgc's advantages: no Node.js required, no install step, works anywhere Python runs, directly integrates with Claude Code launch.

---

## License

MIT
