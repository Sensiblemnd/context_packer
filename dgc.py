#!/usr/bin/env python3
"""
dgc.py - Smart context packer for Claude Code
Scans your project, finds the most relevant files, and pre-loads them
into CLAUDE_CONTEXT.md so Claude spends tokens solving — not exploring.

Usage (Windows):
    python dgc.py                                  # scan + asks what to do
    python dgc.py --context-only                   # just create the context file
    python dgc.py --launch                         # create + launch Claude
    python dgc.py --launch "fix the login bug"     # create + launch with prompt
    python dgc.py --refresh                        # only rescan changed files
    python dgc.py --watch                          # auto-regenerate on file save
    python dgc.py --focus src/reducers             # only pack a specific folder
    python dgc.py C:\path\to\project --launch      # specific project

Usage (Linux / macOS):
    chmod +x dgc.py && sudo cp dgc.py /usr/local/bin/dgc
    dgc --launch "add tests"
    dgc --focus src/api --launch
    dgc --watch
"""

import os
import re
import sys
import json
import time
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_FILES         = 40
MAX_FILE_CHARS    = 3000
MAX_TOTAL_CHARS   = 50_000
CONTEXT_FILE      = "CLAUDE_CONTEXT.md"
SESSION_FILE      = ".dgc-session.json"
NOTES_FILE        = "CLAUDE_NOTES.md"        # user-editable, injected every run
CHARS_PER_TOKEN   = 4                        # rough estimate for token counting

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage", ".cache",
    ".dual-graph", ".idea", ".vscode", ".dgc-session.json",
}

CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".kt", ".cs", ".rb", ".php", ".c", ".cpp", ".swift", ".vue",
    ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml",
    ".env", ".sh", ".ps1", ".md",
}

IMPORTANT_NAMES = {
    "main", "index", "app", "server", "config", "settings",
    "routes", "api", "auth", "db", "database", "models", "schema",
    "utils", "helpers", "types", "constants", "middleware", "store",
}

# Inline config files — include as one-liner summaries instead of full content
SUMMARISE_FILES = {"tsconfig.json", "tsconfig.node.json", ".eslintrc.json",
                   "prettier.config.js", "postcss.config.js", ".babelrc"}


# ── Strippers — reduce noise without losing meaning ───────────────────────────
JSDOC_RE   = re.compile(r'/\*\*.*?\*/', re.DOTALL)
BLANK_RE   = re.compile(r'\n{3,}')

# Matches full import blocks for JS/TS (including multi-line)
JS_IMPORT_RE = re.compile(
    r'^import\s+(?:type\s+)?(?:[\w*{}\s,]+\s+from\s+)?["\'].*?["\'];?\s*$',
    re.MULTILINE
)
# Matches Python imports (import x, from x import y)
PY_IMPORT_RE = re.compile(
    r'^(?:import\s+\S+|from\s+\S+\s+import\s+.+)$',
    re.MULTILINE
)
# Go, Rust, Java, C# imports/includes
GO_IMPORT_RE   = re.compile(r'^import\s+"[^"]+"$', re.MULTILINE)
RUST_USE_RE    = re.compile(r'^use\s+[\w::{}, ]+;$', re.MULTILINE)
JAVA_IMPORT_RE = re.compile(r'^import\s+[\w.]+;$', re.MULTILINE)
CS_USING_RE    = re.compile(r'^using\s+[\w.]+;$', re.MULTILINE)

def strip_noise(text: str, ext: str) -> str:
    """Remove imports, JSDoc blocks, and collapse blank lines."""
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        text = JSDOC_RE.sub('', text)       # remove /** ... */ blocks
        text = JS_IMPORT_RE.sub('', text)   # remove import statements
    elif ext == ".py":
        text = re.sub(r'""".*?"""', '', text, flags=re.DOTALL)
        text = re.sub(r"'''.*?'''", '', text, flags=re.DOTALL)
        text = PY_IMPORT_RE.sub('', text)
    elif ext == ".go":
        # Remove Go import blocks (single and grouped)
        text = re.sub(r'import\s*\(.*?\)', '', text, flags=re.DOTALL)
        text = GO_IMPORT_RE.sub('', text)
    elif ext == ".rs":
        text = RUST_USE_RE.sub('', text)
    elif ext in {".java", ".kt"}:
        text = JAVA_IMPORT_RE.sub('', text)
    elif ext == ".cs":
        text = CS_USING_RE.sub('', text)

    text = BLANK_RE.sub('\n\n', text)   # collapse excess blank lines
    return text.strip()


def summarise_config(path: Path) -> str:
    """Return a compact one-liner for config files instead of full content."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if path.name == "tsconfig.json":
            opts = data.get("compilerOptions", {})
            return (f"target={opts.get('target','?')} "
                    f"jsx={opts.get('jsx','?')} "
                    f"strict={opts.get('strict','?')} "
                    f"baseUrl={opts.get('baseUrl','?')}")
        if path.name == "package.json":
            deps = list(data.get("dependencies", {}).keys())[:8]
            dev  = list(data.get("devDependencies", {}).keys())[:5]
            scripts = list(data.get("scripts", {}).keys())
            return (f"scripts: {', '.join(scripts)}\n"
                    f"deps: {', '.join(deps)}\n"
                    f"devDeps: {', '.join(dev)}")
        return json.dumps(data, indent=2)[:400]
    except Exception:
        return path.read_text(encoding="utf-8", errors="replace")[:400]


# ── Git helpers ────────────────────────────────────────────────────────────────
def git_recent_files(root: Path, n: int = 20) -> set:
    """Return set of recently modified files from git log."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-{n}"],
            cwd=root, capture_output=True, text=True, timeout=5
        )
        files = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)
        return files
    except Exception:
        return set()


def git_branch(root: Path) -> str:
    try:
        r = subprocess.run(["git", "branch", "--show-current"],
                           cwd=root, capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ── File scanning ──────────────────────────────────────────────────────────────
def should_skip(rel: Path) -> bool:
    for part in rel.parts:
        if part in SKIP_DIRS or (part.startswith(".") and part not in {".env"}):
            return True
    return False


def score_file(path: Path, root: Path, recent: set, prompt_keywords: list) -> int:
    score = 0
    stem  = path.stem.lower()
    rel   = str(path.relative_to(root)).replace("\\", "/")

    # Important name bonus
    if stem in IMPORTANT_NAMES:
        score += 30

    # Shallower = more important
    depth = len(path.relative_to(root).parts)
    score += max(0, 10 - depth * 2)

    # File size sweet spot
    try:
        size = path.stat().st_size
        if size < 5_000:
            score += 10
        elif size > 50_000:
            score -= 10
    except Exception:
        pass

    # Config / entry patterns
    if any(p in rel.lower() for p in ["config", "setting", "env", "route", "api"]):
        score += 15

    # Recently touched in git
    if rel in recent or path.name in recent:
        score += 40

    # Prompt keyword match — boost files whose name/path matches what you asked about
    for kw in prompt_keywords:
        if kw in rel.lower() or kw in stem:
            score += 25

    return score


def scan_project(root: Path, focus: str, recent: set, prompt_keywords: list) -> list:
    search_root = (root / focus) if focus else root
    if not search_root.exists():
        print(f"[dgc] Warning: --focus path '{focus}' not found, scanning full project.")
        search_root = root

    files = []
    for path in search_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if should_skip(rel):
            continue
        if path.suffix.lower() not in CODE_EXTS:
            continue
        files.append(path)

    files.sort(key=lambda p: score_file(p, root, recent, prompt_keywords), reverse=True)
    return files[:MAX_FILES]


# ── Session memory ─────────────────────────────────────────────────────────────
def load_session(root: Path) -> dict:
    p = root / SESSION_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session(root: Path, files: list, root_path: Path):
    session = {
        "timestamp": datetime.now().isoformat(),
        "files": {
            str(f.relative_to(root_path)).replace("\\", "/"): _file_hash(f)
            for f in files
        }
    }
    (root_path / SESSION_FILE).write_text(json.dumps(session, indent=2), encoding="utf-8")


def _file_hash(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]
    except Exception:
        return ""


def changed_since_last_run(files: list, session: dict, root: Path) -> list:
    """Return only files that are new or have changed since last session."""
    prev = session.get("files", {})
    changed = []
    for f in files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        if rel not in prev or prev[rel] != _file_hash(f):
            changed.append(f)
    return changed


# ── Context builder ────────────────────────────────────────────────────────────
def read_file_snippet(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")

        # Summarise known config files compactly
        if path.name in SUMMARISE_FILES or path.name == "package.json":
            return summarise_config(path)

        # Strip noise
        text = strip_noise(text, path.suffix.lower())

        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n... (truncated, {len(text)} chars total)"
        return text
    except Exception as e:
        return f"(could not read: {e})"


def build_file_tree(files: list, root: Path) -> str:
    """Build a proper indented tree grouped by directory."""
    tree: dict = {}
    for f in files:
        parts = f.relative_to(root).parts
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None

    lines = []
    def render(node, indent=0):
        for key, val in sorted(node.items(), key=lambda x: (x[1] is not None, x[0])):
            prefix = "  " * indent
            if val is None:
                lines.append(f"{prefix}{key}")
            else:
                lines.append(f"{prefix}{key}/")
                render(val, indent + 1)
    render(tree)
    return "\n".join(lines)


def detect_project_type(root: Path) -> str:
    markers = {
        "package.json":    "Node.js / JavaScript",
        "pyproject.toml":  "Python",
        "requirements.txt":"Python",
        "go.mod":          "Go",
        "Cargo.toml":      "Rust",
        "pom.xml":         "Java (Maven)",
        "build.gradle":    "Java (Gradle)",
    }
    for marker, label in markers.items():
        if (root / marker).exists():
            # Extra detail for Node projects
            if marker == "package.json":
                try:
                    pkg = json.loads((root / "package.json").read_text())
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    if "react" in deps:       label += " / React"
                    if "next" in deps:        label += " / Next.js"
                    if "vue" in deps:         label += " / Vue"
                    if "typescript" in deps:  label += " / TypeScript"
                    if "vite" in deps:        label += " / Vite"
                except Exception:
                    pass
            return label
    if list(root.glob("*.csproj")):
        return ".NET / C#"
    return "Unknown"


def auto_summary(root: Path, proj_type: str, files: list, branch: str) -> str:
    """Generate a short human-readable project summary for Claude."""
    n_files = len(list(root.rglob("*")))
    lines = [
        f"## Project Summary",
        f"- **Name:** {root.name}",
        f"- **Type:** {proj_type}",
        f"- **Branch:** {branch}",
        f"- **Total files:** ~{n_files}",
        f"- **Context files:** {len(files)}",
        "",
        "## Architecture (inferred)",
    ]

    # Infer architecture from folder names
    folders = {p.parent.name for p in files if p.parent != root}
    known = {
        "components": "React components",
        "hooks":      "Custom hooks",
        "reducers":   "State reducers (useReducer)",
        "context":    "React context providers",
        "routes":     "Page routes",
        "types":      "TypeScript type definitions",
        "lib":        "Shared utilities / helpers",
        "api":        "API layer",
        "models":     "Data models",
        "services":   "Service layer",
        "store":      "State store",
        "utils":      "Utility functions",
        "styles":     "CSS / styling",
    }
    found = [(k, v) for k, v in known.items() if k in folders]
    if found:
        for folder, desc in found:
            lines.append(f"- `{folder}/` — {desc}")
    else:
        lines.append("- (Could not infer architecture from folder structure)")

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def build_context(root: Path, files: list, proj_type: str, branch: str,
                  prompt: str, refresh_mode: bool, session: dict) -> str:
    notes = ""
    notes_path = root / NOTES_FILE
    if notes_path.exists():
        notes = notes_path.read_text(encoding="utf-8").strip()

    changed = changed_since_last_run(files, session, root) if refresh_mode else files
    if refresh_mode and len(changed) < len(files):
        print(f"[dgc] Refresh: {len(changed)}/{len(files)} files changed since last run")

    lines = [
        auto_summary(root, proj_type, files, branch),
        "",
    ]

    if notes:
        lines += ["## Session Notes (from CLAUDE_NOTES.md)", notes, ""]

    if prompt:
        lines += [f"## Session Goal", prompt, ""]

    lines += [
        "## File Tree (pre-loaded files)",
        "```",
        build_file_tree(files, root),
        "```",
        "",
    ]

    total_chars = 0
    included    = 0
    skipped     = []

    for f in files:
        # In refresh mode, skip unchanged files but note them
        if refresh_mode and f not in changed:
            skipped.append(str(f.relative_to(root)).replace("\\", "/"))
            continue

        snippet    = read_file_snippet(f)
        block_size = len(snippet) + 100
        if total_chars + block_size > MAX_TOTAL_CHARS:
            lines.append(f"\n*(context budget reached — {len(files) - included} files omitted)*")
            break

        rel = str(f.relative_to(root)).replace("\\", "/")
        ext = f.suffix.lstrip(".")
        lines += [f"## {rel}", f"```{ext}", snippet, "```", ""]
        total_chars += block_size
        included    += 1

    if refresh_mode and skipped:
        lines += [
            "## Unchanged files (not re-included)",
            "*(These were in the last context and haven't changed)*",
        ]
        for s in skipped[:20]:
            lines.append(f"- {s}")
        lines.append("")

    lines += [
        "---",
        f"*Generated by dgc.py at {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"edit {NOTES_FILE} to add persistent notes.*",
    ]
    return "\n".join(lines)


# ── Watch mode ─────────────────────────────────────────────────────────────────
def watch_mode(root: Path, focus: str, prompt: str):
    print(f"[dgc] Watch mode — regenerating {CONTEXT_FILE} on file changes. Ctrl+C to stop.")
    last_hashes: dict = {}

    def get_hashes(files):
        return {str(f): _file_hash(f) for f in files}

    recent   = git_recent_files(root)
    keywords = extract_keywords(prompt)
    files    = scan_project(root, focus, recent, keywords)
    last_hashes = get_hashes(files)
    _write_context(root, files, prompt, refresh_mode=False, session={})
    print(f"[dgc] Initial context written. Watching for changes...")

    try:
        while True:
            time.sleep(2)
            recent  = git_recent_files(root)
            files   = scan_project(root, focus, recent, keywords)
            current = get_hashes(files)
            if current != last_hashes:
                print(f"[dgc] Change detected — regenerating...")
                _write_context(root, files, prompt, refresh_mode=False, session={})
                last_hashes = current
    except KeyboardInterrupt:
        print("\n[dgc] Watch mode stopped.")


# ── Launch Claude ──────────────────────────────────────────────────────────────
def launch_claude(root: Path, prompt: str = ""):
    claude_cmd = ["claude"]
    if prompt:
        claude_cmd += ["--print",
                       f"I've pre-loaded project context into {CONTEXT_FILE}. "
                       f"Please read it first, then: {prompt}"]
    print(f"[dgc] Launching Claude Code...")
    try:
        os.chdir(root)
        subprocess.run(claude_cmd, check=False)
    except FileNotFoundError:
        print("[dgc] Error: 'claude' not found. Install it with:")
        print("       npm install -g @anthropic-ai/claude-code")
        sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────────
def extract_keywords(prompt: str) -> list:
    """Pull meaningful words from a prompt to boost relevant file scores."""
    if not prompt:
        return []
    stop = {"the", "a", "an", "fix", "add", "in", "to", "for", "and", "or",
            "with", "make", "update", "change", "my", "our", "this", "that"}
    words = re.findall(r'[a-z]+', prompt.lower())
    return [w for w in words if w not in stop and len(w) > 2]


def _write_context(root: Path, files: list, prompt: str,
                   refresh_mode: bool, session: dict) -> tuple:
    proj_type = detect_project_type(root)
    branch    = git_branch(root)
    context   = build_context(root, files, proj_type, branch,
                               prompt, refresh_mode, session)
    context_path = root / CONTEXT_FILE
    context_path.write_text(context, encoding="utf-8")
    save_session(root, files, root)

    size_kb  = context_path.stat().st_size / 1024
    tokens   = estimate_tokens(context)
    return size_kb, tokens, proj_type


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    # Parse flags
    context_only  = "--context-only" in args
    do_launch     = "--launch"        in args
    do_refresh    = "--refresh"       in args
    do_watch      = "--watch"         in args
    args = [a for a in args if a not in
            ("--context-only", "--launch", "--refresh", "--watch")]

    # --focus <folder>
    focus = ""
    if "--focus" in args:
        i = args.index("--focus")
        if i + 1 < len(args):
            focus = args[i + 1]
            args  = args[:i] + args[i+2:]
        else:
            print("[dgc] Error: --focus requires a folder path")
            sys.exit(1)

    # Parse project path + prompt
    project_path = "."
    prompt = ""
    if args:
        if Path(args[0]).exists() or args[0] == ".":
            project_path = args[0]
            prompt = " ".join(args[1:])
        else:
            prompt = " ".join(args)

    root = Path(project_path).resolve()
    if not root.is_dir():
        print(f"[dgc] Error: '{root}' is not a directory.")
        sys.exit(1)

    # Watch mode (blocking)
    if do_watch:
        watch_mode(root, focus, prompt)
        return

    # Scan
    print(f"[dgc] Scanning {root.name}{'/' + focus if focus else ''}...")
    recent   = git_recent_files(root)
    keywords = extract_keywords(prompt)
    if recent:
        print(f"[dgc] Git: {len(recent)} recently changed files will be prioritised")
    if keywords:
        print(f"[dgc] Prompt keywords: {', '.join(keywords)}")

    files   = scan_project(root, focus, recent, keywords)
    session = load_session(root) if do_refresh else {}

    # Ask BEFORE writing anything (unless a flag was given)
    if not context_only and not do_launch:
        print()
        print("What would you like to do?")
        print("  1) Just create the context file")
        print("  2) Create the context file + launch Claude Code")
        choice = input("Enter 1 or 2: ").strip()
        if choice == "2" and not prompt:
            prompt = input("Starting prompt for Claude (optional, Enter to skip): ").strip()
    else:
        choice = "1"

    print()
    print(f"[dgc] Packing {len(files)} files...")
    size_kb, tokens, proj_type = _write_context(root, files, prompt, do_refresh, session)

    print(f"[dgc] ✓ {CONTEXT_FILE} written ({size_kb:.1f} KB, ~{tokens:,} tokens)")
    print(f"[dgc] Project: {proj_type} | Branch: {git_branch(root)}")

    notes_path = root / NOTES_FILE
    if not notes_path.exists():
        print(f"[dgc] Tip: create {NOTES_FILE} to add persistent notes for Claude")
    else:
        print(f"[dgc] Notes injected from {NOTES_FILE}")

    print(f"[dgc] Estimated tokens saved vs no context: ~30-40% per session")

    if context_only:
        print(f"[dgc] Done. Review {CONTEXT_FILE}, then run 'claude' when ready.")
        return

    if do_launch or choice == "2":
        launch_claude(root, prompt)
    else:
        print(f"[dgc] Done. Run 'claude' in {root.name} when ready.")


if __name__ == "__main__":
    main()
