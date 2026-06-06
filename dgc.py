#!/usr/bin/env python3
"""
dgc.py - Smart context packer for Claude Code

Usage:
    python dgc.py                                   # interactive
    python dgc.py --context-only                    # just write the file
    python dgc.py --launch                          # write + launch Claude
    python dgc.py --launch "fix the login bug"      # write + launch with prompt
    python dgc.py --refresh                         # only repack changed files
    python dgc.py --focus src/reducers              # only pack a subfolder
    python dgc.py --watch                           # regenerate on file changes

Optional dependencies (pip install tiktoken watchdog):
    tiktoken  — accurate token counting instead of char/4 estimate
    watchdog  — efficient file-system events instead of polling
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Optional deps ──────────────────────────────────────────────────────────────
try:
    import tiktoken as _tiktoken
    def count_tokens(text: str) -> int:
        try:
            enc = _tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4
    _HAS_TIKTOKEN = True
except ImportError:
    _tiktoken = None  # type: ignore
    _HAS_TIKTOKEN = False
    def count_tokens(text: str) -> int:
        return len(text) // 4

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_FILES       = 50
MAX_FILE_CHARS  = 3000
MAX_TOTAL_CHARS = 75_000

CONTEXT_FILE = "CLAUDE_CONTEXT.md"
SESSION_FILE = ".dgc-session.json"
NOTES_FILE   = "CLAUDE_NOTES.md"

GENERATED_FILES = {CONTEXT_FILE, SESSION_FILE}

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "coverage",
    ".next", ".nuxt", ".cache", ".idea", ".vscode",
    "__pycache__", ".venv", "venv", "env",
}

SECRET_NAMES = {
    ".env", ".env.local", ".env.production",
    ".env.development", ".env.test",
}

CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".kt", ".cs", ".json", ".yaml", ".yml",
    ".toml", ".md", ".html", ".css", ".scss", ".sh", ".ps1",
}

SUMMARISE_FILES = {
    "tsconfig.json", "tsconfig.node.json", ".eslintrc.json",
    "prettier.config.js", "postcss.config.js", ".babelrc", "package.json",
}

STOP_WORDS = {
    "the", "a", "an", "fix", "add", "change", "update", "get", "set",
    "use", "new", "old", "all", "create", "remove", "delete",
    "implement", "refactor", "make", "with", "for", "and", "or",
    "this", "that", "our", "my", "in", "to",
}

IMPORTANT_NAMES = {
    "main", "index", "app", "server", "config", "settings",
    "routes", "api", "auth", "db", "database", "models", "schema",
    "utils", "helpers", "types", "constants", "middleware", "store",
}

ARCH_FOLDERS = {
    "components": "React components",
    "hooks":      "Custom hooks",
    "reducers":   "State reducers",
    "context":    "React context providers",
    "routes":     "Page routes",
    "types":      "TypeScript types",
    "lib":        "Shared utilities",
    "api":        "API layer",
    "models":     "Data models",
    "services":   "Service layer",
    "store":      "State store",
    "utils":      "Utility functions",
    "styles":     "CSS / styling",
}


# ── Data ───────────────────────────────────────────────────────────────────────
@dataclass
class ContextStats:
    included: int = 0
    omitted:  int = 0
    chars:    int = 0
    tokens:   int = 0


@dataclass
class ScanResult:
    files:    list[Path]
    recent:   set[str]
    keywords: list[str]
    freq:     dict[str, int] = field(default_factory=dict)


# ── Filesystem helpers ─────────────────────────────────────────────────────────
def should_skip(rel: Path) -> bool:
    if rel.name in GENERATED_FILES or rel.name in SECRET_NAMES:
        return True
    for part in rel.parts:
        if part in SKIP_DIRS or (part.startswith(".") and part not in SECRET_NAMES):
            return True
    return False


def hash_file(path: Path) -> str:
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            while chunk := f.read(65_536):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:8]


# ── Git helpers ────────────────────────────────────────────────────────────────
def git_recent_files(root: Path, n: int = 20) -> set[str]:
    try:
        r = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-{n}"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        return {x.strip().replace("\\", "/") for x in r.stdout.splitlines() if x.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def git_branch(root: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root, capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def git_file_frequency(root: Path, n: int = 100) -> dict[str, int]:
    """Count how many commits each file appears in (= churn = importance)."""
    try:
        r = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-{n}"],
            cwd=root, capture_output=True, text=True, timeout=8,
        )
        freq: dict[str, int] = {}
        for line in r.stdout.splitlines():
            line = line.strip().replace("\\", "/")
            if line:
                freq[line] = freq.get(line, 0) + 1
        return freq
    except (OSError, subprocess.SubprocessError):
        return {}


# ── Strippers ──────────────────────────────────────────────────────────────────
_JSDOC_RE         = re.compile(r"/\*\*.*?\*/", re.DOTALL)
_JS_IMPORT_RE     = re.compile(r"^import\s[^\n]+$", re.MULTILINE)
_BLANK_RE         = re.compile(r"\n{3,}")
_GO_BLOCK_RE      = re.compile(r"import\s*\(.*?\)", re.DOTALL)
_GO_LINE_RE       = re.compile(r'^import\s+"[^"]+"$', re.MULTILINE)
_RUST_USE_RE      = re.compile(r"^use\s+[\w::{}\s,*]+;$", re.MULTILINE)
_JAVA_IMPORT_RE   = re.compile(r"^import\s+[\w.*]+;$", re.MULTILINE)
_CS_USING_RE      = re.compile(r"^using\s+[\w.]+;$", re.MULTILINE)


def strip_python(text: str) -> str:
    """AST-based docstring removal + import strip for Python."""
    try:
        tree = ast.parse(text)
        lines = text.splitlines()

        def blank_docstring(node: ast.AST) -> None:
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                for i in range(body[0].lineno - 1, body[0].end_lineno):
                    lines[i] = ""

        blank_docstring(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                blank_docstring(node)

        body_lines = [
            ln for ln in lines
            if not re.match(r"^\s*(from\s+\S+\s+import|import\s+)", ln)
        ]
        return _BLANK_RE.sub("\n\n", "\n".join(body_lines)).strip()
    except SyntaxError:
        return text


def strip_noise(text: str, ext: str) -> str:
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        text = _JSDOC_RE.sub("", text)
        text = _JS_IMPORT_RE.sub("", text)
    elif ext == ".py":
        return strip_python(text)
    elif ext == ".go":
        text = _GO_BLOCK_RE.sub("", text)
        text = _GO_LINE_RE.sub("", text)
    elif ext == ".rs":
        text = _RUST_USE_RE.sub("", text)
    elif ext in {".java", ".kt"}:
        text = _JAVA_IMPORT_RE.sub("", text)
    elif ext == ".cs":
        text = _CS_USING_RE.sub("", text)

    return _BLANK_RE.sub("\n\n", text).strip()


# ── Config summarisers ─────────────────────────────────────────────────────────
def summarise_config(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            return "(could not read)"

    if path.name == "package.json":
        deps    = list(data.get("dependencies", {}).keys())[:8]
        dev     = list(data.get("devDependencies", {}).keys())[:5]
        scripts = list(data.get("scripts", {}).keys())
        return (
            f"scripts: {', '.join(scripts)}\n"
            f"deps: {', '.join(deps)}\n"
            f"devDeps: {', '.join(dev)}"
        )

    if "tsconfig" in path.name:
        opts = data.get("compilerOptions", {})
        return (
            f"target={opts.get('target', '?')}  "
            f"jsx={opts.get('jsx', '?')}  "
            f"strict={opts.get('strict', '?')}  "
            f"baseUrl={opts.get('baseUrl', '.')}"
        )

    return json.dumps(data, indent=2)[:400]


# ── File reading ───────────────────────────────────────────────────────────────
def read_snippet(path: Path) -> str:
    try:
        if path.name in SUMMARISE_FILES:
            return summarise_config(path)

        text = path.read_text(encoding="utf-8", errors="replace")
        text = strip_noise(text, path.suffix.lower())

        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n... (truncated — {len(text)} chars total)"

        return text
    except OSError as e:
        return f"(unable to read: {e})"


# ── Scoring ────────────────────────────────────────────────────────────────────
def score_file(
    path: Path, root: Path,
    recent: set[str], keywords: list[str], freq: dict[str, int],
) -> int:
    score = 0
    rel   = str(path.relative_to(root)).replace("\\", "/")
    stem  = path.stem.lower()

    # Git recency
    if rel in recent:
        score += 50

    # Git frequency (churn) — files touched often are architecturally important
    score += min(freq.get(rel, 0) * 5, 30)

    # Important name
    if stem in IMPORTANT_NAMES:
        score += 30

    # Shallow depth
    depth = len(path.relative_to(root).parts)
    score += max(0, 15 - depth * 2)

    # Config / entry patterns
    if any(p in rel.lower() for p in ["config", "route", "api", "setting"]):
        score += 15

    # File size sweet spot
    try:
        size = path.stat().st_size
        if size < 5_000:
            score += 10
        elif size > 50_000:
            score -= 10
    except OSError:
        pass

    # Prompt keyword match
    for kw in keywords:
        if kw in rel.lower():
            score += 25

    return score


# ── Scanning ───────────────────────────────────────────────────────────────────
def extract_keywords(prompt: str) -> list[str]:
    words = re.findall(r"[a-z]+", prompt.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 2]


def scan_project(root: Path, focus: str, prompt: str) -> ScanResult:
    search_root = (root / focus) if focus else root
    if not search_root.is_dir():
        print(f"[dgc] Warning: --focus '{focus}' not found, scanning full project.")
        search_root = root

    recent   = git_recent_files(root)
    keywords = extract_keywords(prompt)
    freq     = git_file_frequency(root)

    files: list[Path] = []
    for p in search_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if should_skip(rel):
            continue
        if p.suffix.lower() not in CODE_EXTS:
            continue
        files.append(p)

    files.sort(
        key=lambda p: score_file(p, root, recent, keywords, freq),
        reverse=True,
    )
    return ScanResult(files=files[:MAX_FILES], recent=recent, keywords=keywords, freq=freq)


# ── Session memory ─────────────────────────────────────────────────────────────
def load_session(root: Path) -> dict:
    try:
        return json.loads((root / SESSION_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_session(root: Path, files: list[Path]) -> None:
    data = {
        "timestamp": datetime.now().isoformat(),
        "files": {
            str(f.relative_to(root)).replace("\\", "/"): hash_file(f)
            for f in files
        },
    }
    try:
        (root / SESSION_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[dgc] Warning: could not save session: {e}")


def changed_files(files: list[Path], session: dict, root: Path) -> list[Path]:
    prev = session.get("files", {})
    return [
        f for f in files
        if str(f.relative_to(root)).replace("\\", "/") not in prev
        or prev[str(f.relative_to(root)).replace("\\", "/")] != hash_file(f)
    ]


# ── Tree builder ───────────────────────────────────────────────────────────────
def build_tree(files: list[Path], root: Path) -> str:
    tree: dict = {}
    for f in files:
        parts = f.relative_to(root).parts
        node = tree
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if parts[-1] not in node:
            node[parts[-1]] = None

    lines: list[str] = []

    def walk(node: dict, depth: int = 0) -> None:
        dirs  = sorted(k for k, v in node.items() if isinstance(v, dict))
        leafs = sorted(k for k, v in node.items() if v is None)
        for k in dirs:
            lines.append("  " * depth + k + "/")
            walk(node[k], depth + 1)
        for k in leafs:
            lines.append("  " * depth + k)

    walk(tree)
    return "\n".join(lines)


# ── Summary ────────────────────────────────────────────────────────────────────
def auto_summary(root: Path, files: list[Path], branch: str) -> str:
    try:
        total = sum(
            1 for p in root.rglob("*")
            if p.is_file() and not should_skip(p.relative_to(root))
        )
    except OSError:
        total = 0

    folders = {p.parent.name for p in files if p.parent != root}
    arch = [
        f"- `{k}/` — {v}" for k, v in ARCH_FOLDERS.items() if k in folders
    ] or ["- (architecture not inferred)"]

    proj_type = detect_project_type(root)

    return "\n".join([
        "## Project Summary",
        f"- **Name:** {root.name}",
        f"- **Type:** {proj_type}",
        f"- **Branch:** {branch}",
        f"- **Total files:** ~{total}",
        f"- **Context files:** {len(files)}",
        "",
        "## Architecture",
        *arch,
    ])


def detect_project_type(root: Path) -> str:
    markers = {
        "package.json":    "Node.js",
        "pyproject.toml":  "Python",
        "requirements.txt":"Python",
        "go.mod":          "Go",
        "Cargo.toml":      "Rust",
        "pom.xml":         "Java (Maven)",
        "build.gradle":    "Java (Gradle)",
    }
    for marker, label in markers.items():
        if (root / marker).exists():
            if marker == "package.json":
                try:
                    pkg  = json.loads((root / "package.json").read_text(encoding="utf-8"))
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    extras = [k for k in ["react", "next", "vue", "typescript", "vite"] if k in deps]
                    if extras:
                        label += " / " + " / ".join(e.capitalize() for e in extras)
                except (OSError, json.JSONDecodeError):
                    pass
            return label
    if list(root.glob("*.csproj")):
        return ".NET / C#"
    return "Unknown"


# ── Context builder ────────────────────────────────────────────────────────────
def build_context(
    root: Path,
    result: ScanResult,
    prompt: str,
    refresh_mode: bool,
    session: dict,
    branch: str,
) -> tuple[str, ContextStats]:
    stats = ContextStats()

    notes = ""
    try:
        notes_path = root / NOTES_FILE
        if notes_path.exists():
            notes = notes_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    only_changed = changed_files(result.files, session, root) if refresh_mode else result.files
    if refresh_mode:
        n_changed = len(only_changed)
        n_total   = len(result.files)
        if n_changed < n_total:
            print(f"[dgc] Refresh: {n_changed}/{n_total} files changed")

    lines: list[str] = [auto_summary(root, result.files, branch), ""]

    if notes:
        lines += ["## Session Notes", notes, ""]

    if prompt:
        lines += ["## Session Goal", prompt, ""]

    lines += [
        "## File Tree",
        "```",
        build_tree(result.files, root),
        "```",
        "",
    ]

    skipped: list[str] = []

    for f in result.files:
        rel = str(f.relative_to(root)).replace("\\", "/")

        if refresh_mode and f not in only_changed:
            skipped.append(rel)
            continue

        snippet   = read_snippet(f)
        block_len = len(snippet) + 100

        if stats.chars + block_len > MAX_TOTAL_CHARS:
            stats.omitted += 1
            continue

        stats.chars += block_len
        stats.included += 1
        ext = f.suffix.lstrip(".")
        lines += [f"## {rel}", f"```{ext}", snippet, "```", ""]

    if stats.omitted:
        lines.append(f"*Budget reached — {stats.omitted} file(s) omitted.*\n")

    if refresh_mode and skipped:
        lines += ["## Unchanged (omitted from refresh)", ""]
        lines += [f"- {s}" for s in skipped[:20]]
        lines.append("")

    lines += [
        "---",
        f"*dgc.py — {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"edit {NOTES_FILE} for persistent notes*",
    ]

    text = "\n".join(lines)
    stats.tokens = count_tokens(text)
    return text, stats


# ── Write ──────────────────────────────────────────────────────────────────────
def write_context(
    root: Path, result: ScanResult, prompt: str,
    refresh_mode: bool, session: dict,
) -> ContextStats:
    branch = git_branch(root)
    text, stats = build_context(root, result, prompt, refresh_mode, session, branch)
    (root / CONTEXT_FILE).write_text(text, encoding="utf-8")
    save_session(root, result.files)
    return stats


# ── Watch mode ─────────────────────────────────────────────────────────────────
def watch_mode(root: Path, focus: str, prompt: str) -> None:
    print(f"[dgc] Watch mode active. Ctrl+C to stop.")

    def regenerate() -> None:
        result = scan_project(root, focus, prompt)
        stats  = write_context(root, result, prompt, refresh_mode=False, session={})
        print(f"[dgc] Regenerated — {stats.included} files, ~{stats.tokens:,} tokens "
              f"({datetime.now().strftime('%H:%M:%S')})")

    regenerate()

    if _HAS_WATCHDOG:
        class Handler(FileSystemEventHandler):
            def __init__(self):
                self._last = 0.0
            def on_any_event(self, event):
                if event.is_directory:
                    return
                now = time.monotonic()
                if now - self._last < 1.5:   # debounce
                    return
                self._last = now
                regenerate()

        observer = Observer()
        observer.schedule(Handler(), str(root), recursive=True)
        observer.start()
        print("[dgc] Using watchdog for efficient file-system events.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        # Polling fallback
        print("[dgc] watchdog not installed — polling every 2s (pip install watchdog to improve).")
        last: dict[str, str] = {}

        def snapshot() -> dict[str, str]:
            result = scan_project(root, focus, prompt)
            return {str(f): hash_file(f) for f in result.files}

        last = snapshot()
        try:
            while True:
                time.sleep(2)
                current = snapshot()
                if current != last:
                    regenerate()
                    last = current
        except KeyboardInterrupt:
            print("\n[dgc] Watch stopped.")


# ── Launch Claude ──────────────────────────────────────────────────────────────
def launch_claude(root: Path, prompt: str = "") -> None:
    cmd = ["claude"]
    if prompt:
        cmd += [
            "--print",
            f"Project context is in {CONTEXT_FILE}. Please read it first, then: {prompt}",
        ]
    print("[dgc] Launching Claude Code...")
    try:
        os.chdir(root)
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print("[dgc] 'claude' not found. Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    args = sys.argv[1:]

    context_only = "--context-only" in args
    do_launch    = "--launch"        in args
    do_refresh   = "--refresh"       in args
    do_watch     = "--watch"         in args
    args = [a for a in args if a not in ("--context-only", "--launch", "--refresh", "--watch")]

    focus = ""
    if "--focus" in args:
        i = args.index("--focus")
        if i + 1 < len(args):
            focus = args[i + 1]
            args  = args[:i] + args[i + 2:]
        else:
            print("[dgc] --focus requires a folder argument")
            sys.exit(1)

    prompt = " ".join(args).strip()
    root   = Path.cwd()

    if do_watch:
        watch_mode(root, focus, prompt)
        return

    # Scan first (fast)
    print(f"[dgc] Scanning {root.name}{'/' + focus if focus else ''}...")
    result = scan_project(root, focus, prompt)

    if result.recent:
        print(f"[dgc] Git: {len(result.recent)} recently changed files prioritised")
    if result.keywords:
        print(f"[dgc] Keywords: {', '.join(result.keywords)}")

    token_lib = "tiktoken" if _HAS_TIKTOKEN else "char/4 estimate"
    print(f"[dgc] Token counting: {token_lib}")

    session = load_session(root) if do_refresh else {}

    # Ask BEFORE writing (interactive mode only)
    choice = "1"
    if not context_only and not do_launch:
        print()
        print("What would you like to do?")
        print("  1) Just create the context file")
        print("  2) Create the context file + launch Claude Code")
        choice = input("Enter 1 or 2: ").strip()
        if choice == "2" and not prompt:
            prompt = input("Starting prompt for Claude (optional, Enter to skip): ").strip()

    print()
    print(f"[dgc] Packing {len(result.files)} files...")
    stats = write_context(root, result, prompt, do_refresh, session)

    size_kb = (root / CONTEXT_FILE).stat().st_size / 1024
    print(f"[dgc] ✓ {CONTEXT_FILE} — {size_kb:.1f} KB, ~{stats.tokens:,} tokens")
    print(f"[dgc] {stats.included} files included, {stats.omitted} omitted")
    print(f"[dgc] Branch: {git_branch(root)}")

    if not (root / NOTES_FILE).exists():
        print(f"[dgc] Tip: create {NOTES_FILE} for persistent per-project notes")
    else:
        print(f"[dgc] Notes injected from {NOTES_FILE}")

    if context_only:
        print(f"[dgc] Done. Run 'claude' when ready.")
        return

    if do_launch or choice == "2":
        launch_claude(root, prompt)
    else:
        print(f"[dgc] Done. Run 'claude' in {root.name} when ready.")


if __name__ == "__main__":
    main()
