# dgc — Context Packer for Claude Code

Pre-loads your codebase into a single `CLAUDE_CONTEXT.md` file so Claude Code spends tokens **solving**, not exploring. Saves ~30–40% of tokens per session.

No license server. No telemetry. No dependencies beyond Python stdlib.

---

## How it works

1. Scans your project and scores every file by importance
2. Asks what you want to do
3. Strips noise (imports, JSDoc, docstrings) and packs the most relevant files into `CLAUDE_CONTEXT.md`
4. Optionally launches Claude Code with that context pre-loaded

Claude reads the file at the start of the session instead of spending 5–10 turns exploring your codebase with tool calls.

---

## Requirements

- Python 3.8+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- Git (optional, improves file scoring)

---

## Install

**Windows:**
```powershell
# Just download dgc.py and run it
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
# Basic — scan current directory, asks what to do
dgc
python dgc.py

# Specific project
dgc /path/to/project
python dgc.py C:\path\to\project

# Just create the context file, don't launch Claude
dgc --context-only

# Create context + launch Claude immediately
dgc --launch
dgc --launch "fix the login bug"

# Only rescan files that changed since last run
dgc --refresh --launch

# Only pack a specific folder
dgc --focus src/reducers --launch

# Auto-regenerate context whenever files change
dgc --watch
```

---

## File scoring

Files are ranked by a scoring system — higher score = included first:

| Signal | Boost |
|---|---|
| Recently changed (`git log`) | +40 |
| Name matches prompt keywords | +25 |
| Important name (`index`, `app`, `config`, `routes`, `api`…) | +30 |
| Config/entry point path pattern | +15 |
| Shallow depth in project | up to +10 |
| Small file (< 5 KB) | +10 |
| Large file (> 50 KB) | −10 |

---

## Noise stripping

The following are removed before packing to reduce token waste:

- `import` / `from` statements (TS, JS, Python, Go, Rust, Java, C#)
- JSDoc `/** */` blocks
- Python triple-quoted docstrings
- Excess blank lines

Config files (`tsconfig.json`, `package.json`, etc.) are summarised as compact one-liners instead of included in full.

---

## Persistent notes

Create a `CLAUDE_NOTES.md` file in your project root. It gets injected into every context file automatically — useful for things like known issues, architecture decisions, or anything you always want Claude to know.

```markdown
# CLAUDE_NOTES.md

## Architecture
- State is managed entirely via useReducer + Context, no external state lib
- All reducer handlers are split into individual files under src/reducers/

## Known issues
- handle-tick.ts has a race condition when legacy plant dies on the same tick as a standup
```

---

## Session memory

After each run, `dgc` saves a `.dgc-session.json` file with a hash of every packed file. Use `--refresh` on the next run to skip files that haven't changed and only re-pack what's new.

---

## Output files

| File | Description |
|---|---|
| `CLAUDE_CONTEXT.md` | Generated context — read by Claude at session start |
| `CLAUDE_NOTES.md` | Your persistent notes — edit this manually |
| `.dgc-session.json` | Session cache for `--refresh` mode |

`CLAUDE_CONTEXT.md` and `.dgc-session.json` should be added to `.gitignore`.

```bash
echo "CLAUDE_CONTEXT.md" >> .gitignore
echo ".dgc-session.json" >> .gitignore
```

---

## License

MIT
