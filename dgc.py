#!/usr/bin/env python3
"""
dgc.py - Smart context packer for Claude Code

Usage:
    python dgc.py                                        # interactive
    python dgc.py --context-only                         # just write the file
    python dgc.py --launch                               # write + launch Claude
    python dgc.py --launch "fix the login bug"           # write + launch with prompt
    python dgc.py --refresh                              # only repack changed files
    python dgc.py --focus src/reducers                   # only pack a subfolder
    python dgc.py --watch                                # regenerate on file changes
    python dgc.py --exclude "**/*.test.ts" --exclude DOC/  # exclude patterns
    python dgc.py --diverse                              # balance folder representation

Optional dependencies (pip install tiktoken watchdog pathspec):
    tiktoken  - accurate token counting instead of char/4 estimate
    watchdog  - efficient file-system events instead of polling
    pathspec  - .gitignore support (recommended)
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
# Encoder created once at import — reusing it is ~10x faster than re-creating
# per call (tiktoken fetches BPE data on first instantiation).
try:
    import tiktoken as _tiktoken
    _ENC = _tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        try:
            return len(_ENC.encode(text, disallowed_special=()))
        except Exception:
            return len(text) // 4
    _HAS_TIKTOKEN = True
except Exception:
    _tiktoken = None   # type: ignore
    _ENC      = None   # type: ignore
    _HAS_TIKTOKEN = False
    def count_tokens(text: str) -> int:
        return len(text) // 4

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False

try:
    import pathspec as _pathspec
    _HAS_PATHSPEC = True
except ImportError:
    _pathspec     = None  # type: ignore
    _HAS_PATHSPEC = False


# ── Config ─────────────────────────────────────────────────────────────────────
MAX_FILES        = 50
MAX_FILE_TOKENS  = 600    # per-file token cap
MAX_TOTAL_TOKENS = 18_000 # total context budget

CONTEXT_FILE = "CLAUDE_CONTEXT.md"
SESSION_FILE = ".dgc-session.json"
NOTES_FILE   = "CLAUDE_NOTES.md"

GENERATED_FILES = {CONTEXT_FILE, SESSION_FILE}

# Fallback when pathspec is unavailable
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "coverage",
    ".next", ".nuxt", ".cache", ".idea", ".vscode",
    "__pycache__", ".venv", "venv", "env",
}

SECRET_NAMES = {
    ".env", ".env.local", ".env.production",
    ".env.development", ".env.test", ".env.staging",
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

# Max files per directory in --diverse mode before lower-scored dirs get priority
DIVERSE_DIR_CAP = 5


# ── Data ───────────────────────────────────────────────────────────────────────
@dataclass
class ContextStats:
    included: int = 0
    omitted:  int = 0
    tokens:   int = 0


@dataclass
class ScanResult:
    files:    list[Path]
    recent:   set[str]
    keywords: list[str]
    freq:     dict[str, int] = field(default_factory=dict)


@dataclass
class RefreshDiff:
    modified: list[Path]
    added:    list[Path]
    removed:  list[str]   # relative path strings — files no longer on disk

    @property
    def changed(self) -> list[Path]:
        return self.modified + self.added

    def total(self) -> int:
        return len(self.modified) + len(self.added) + len(self.removed)


# ── Hashing — BLAKE2b replaces MD5 ────────────────────────────────────────────
# BLAKE2b is faster than MD5 on 64-bit systems and has no legacy CVEs.
# digest_size=8 keeps hashes short (16 hex chars) for session files.
def hash_file(path: Path) -> str:
    h = hashlib.blake2b(digest_size=8)
    try:
        with path.open("rb") as f:
            while chunk := f.read(65_536):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


# ── .gitignore / pathspec support ─────────────────────────────────────────────
def load_gitignore(root: Path) -> object | None:
    """
    Load .gitignore + .dgcignore into a compiled pathspec matcher.
    Compiled once at startup and reused for every file — avoids repeated
    PathSpec.from_lines() calls that were O(files * patterns).
    Returns None when pathspec is unavailable or no ignore files exist.
    """
    if not _HAS_PATHSPEC:
        return None

    patterns: list[str] = list(GENERATED_FILES)  # always ignore our own output

    for ignore_file in (".gitignore", ".dgcignore"):
        p = root / ignore_file
        if p.exists():
            try:
                patterns += p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                pass

    if not patterns:
        return None

    try:
        return _pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    except Exception:
        return None


def compile_excludes(excludes: list[str]) -> object | None:
    """
    Compile user --exclude patterns into a single PathSpec.
    Called once at startup — reused for every file during scanning.
    Without this, PathSpec.from_lines() was called per-file (O(n*m) allocations).
    """
    if not excludes or not _HAS_PATHSPEC:
        return None
    try:
        return _pathspec.PathSpec.from_lines("gitwildmatch", excludes)
    except Exception:
        return None


def should_skip_fallback(rel: Path) -> bool:
    """Hardcoded skip logic — used when pathspec is unavailable."""
    if rel.name in GENERATED_FILES or rel.name in SECRET_NAMES:
        return True
    for part in rel.parts:
        if part in SKIP_DIRS:
            return True
        if part.startswith(".") and part not in SECRET_NAMES:
            return True
    return False


def should_skip(
    rel: Path,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
) -> bool:
    """
    Master skip check. Evaluated in priority order:
    1. Always block secrets
    2. User --exclude patterns (compiled spec or fallback substring)
    3. .gitignore / .dgcignore (pathspec)
    4. Hardcoded fallback list
    """
    if rel.name in SECRET_NAMES:
        return True

    rel_posix = rel.as_posix()

    # --exclude patterns — use pre-compiled spec when available
    if exclude_spec is not None:
        if exclude_spec.match_file(rel_posix):  # type: ignore[attr-defined]
            return True
    elif raw_excludes:
        # Fallback: simple substring match when pathspec not installed
        for pat in raw_excludes:
            clean = pat.strip("/").replace("**", "")
            if clean and clean in rel_posix:
                return True

    if gitignore is not None:
        return gitignore.match_file(rel_posix)  # type: ignore[attr-defined]

    return should_skip_fallback(rel)


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
    """Commit frequency per file — higher churn = architecturally important."""
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
_JSDOC_RE       = re.compile(r"/\*\*.*?\*/", re.DOTALL)
_JS_IMPORT_RE   = re.compile(r"^import\s[^\n]+$", re.MULTILINE)
_BLANK_RE       = re.compile(r"\n{3,}")
_GO_BLOCK_RE    = re.compile(r"import\s*\(.*?\)", re.DOTALL)
_GO_LINE_RE     = re.compile(r'^import\s+"[^"]+"$', re.MULTILINE)
_RUST_USE_RE    = re.compile(r"^use\s+[\w::{}\s,*]+;$", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^import\s+[\w.*]+;$", re.MULTILINE)
_CS_USING_RE    = re.compile(r"^using\s+[\w.]+;$", re.MULTILINE)
_PY_IMPORT_RE   = re.compile(r"^\s*(from\s+\S+\s+import|import\s+)", re.MULTILINE)


def strip_python(text: str) -> str:
    """AST-based docstring removal + import strip. Falls back to regex on SyntaxError."""
    try:
        tree  = ast.parse(text)
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

        body_lines = [ln for ln in lines if not _PY_IMPORT_RE.match(ln)]
        return _BLANK_RE.sub("\n\n", "\n".join(body_lines)).strip()

    except SyntaxError:
        # Partial treatment — at least strip imports via regex
        stripped = _PY_IMPORT_RE.sub("", text)
        return _BLANK_RE.sub("\n\n", stripped).strip()


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
def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """
    Binary-search truncation that guarantees count_tokens(result) <= max_tokens.
    The char-ratio estimate was inaccurate for code with many non-ASCII or
    dense token sequences. This ensures the budget check is always exact.
    """
    if count_tokens(text) <= max_tokens:
        return text

    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid

    return text[:lo] + "\n... (truncated)"


def read_snippet(path: Path) -> tuple[str, int]:
    """Return (stripped_text, token_count). Token count drives budget tracking."""
    try:
        if path.name in SUMMARISE_FILES:
            text = summarise_config(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            text = strip_noise(text, path.suffix.lower())

        # Binary-search truncation — exact token guarantee
        text   = _truncate_to_tokens(text, MAX_FILE_TOKENS)
        tokens = count_tokens(text)
        return text, tokens

    except OSError as e:
        msg = f"(unable to read: {e})"
        return msg, count_tokens(msg)


# ── Scoring ────────────────────────────────────────────────────────────────────
def score_file(
    path: Path, root: Path,
    recent: set[str], keywords: list[str], freq: dict[str, int],
) -> int:
    score   = 0
    rel_str = path.relative_to(root).as_posix()
    stem    = path.stem.lower()

    if rel_str in recent:
        score += 50
    score += min(freq.get(rel_str, 0) * 5, 30)
    if stem in IMPORTANT_NAMES:
        score += 30

    depth = len(path.relative_to(root).parts)
    score += max(0, 15 - depth * 2)

    if any(p in rel_str for p in ("config", "route", "api", "setting")):
        score += 15

    try:
        size = path.stat().st_size
        if size < 5_000:
            score += 10
        elif size > 50_000:
            score -= 10
    except OSError:
        pass

    for kw in keywords:
        if kw in rel_str:
            score += 25

    return score


# ── Scanning ───────────────────────────────────────────────────────────────────
def extract_keywords(prompt: str) -> list[str]:
    words = re.findall(r"[a-z]+", prompt.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 2]


def _gather_files(
    search_root: Path,
    root: Path,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
) -> list[Path]:
    """Collect all scannable files — shared by scan_project and watch polling."""
    files: list[Path] = []
    for p in search_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if should_skip(rel, gitignore, exclude_spec, raw_excludes):
            continue
        if p.suffix.lower() not in CODE_EXTS:
            continue
        files.append(p)
    return files


def _apply_diversity(files: list[Path], root: Path, cap: int) -> list[Path]:
    """
    Optional --diverse mode: cap files per directory so one hot folder
    can't crowd out the rest of the architecture.
    Files are already sorted by score; we keep the highest-scoring N per dir.
    """
    dir_counts: dict[str, int] = {}
    selected: list[Path] = []
    deferred: list[Path] = []

    for f in files:
        parent = f.relative_to(root).parent.as_posix()
        count  = dir_counts.get(parent, 0)
        if count < cap:
            selected.append(f)
            dir_counts[parent] = count + 1
        else:
            deferred.append(f)

    # Fill remaining slots with deferred files (score-ordered)
    remaining = MAX_FILES - len(selected)
    return selected + deferred[:remaining]


def scan_project(
    root: Path,
    focus: str,
    prompt: str,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
    diverse: bool = False,
) -> ScanResult:
    search_root = (root / focus) if focus else root
    if not search_root.is_dir():
        print(f"[dgc] Warning: --focus '{focus}' not found, scanning full project.")
        search_root = root

    recent   = git_recent_files(root)
    keywords = extract_keywords(prompt)
    freq     = git_file_frequency(root)

    files = _gather_files(search_root, root, gitignore, exclude_spec, raw_excludes)
    files.sort(key=lambda p: score_file(p, root, recent, keywords, freq), reverse=True)

    if diverse:
        files = _apply_diversity(files, root, DIVERSE_DIR_CAP)
    else:
        files = files[:MAX_FILES]

    return ScanResult(files=files, recent=recent, keywords=keywords, freq=freq)


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
            f.relative_to(root).as_posix(): hash_file(f)
            for f in files
        },
    }
    try:
        (root / SESSION_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[dgc] Warning: could not save session: {e}")


def compute_refresh_diff(files: list[Path], session: dict, root: Path) -> RefreshDiff:
    """
    Full diff between current scan and last session:
    - modified: exists now, hash changed
    - added:    exists now, not in session
    - removed:  was in session, no longer on disk
    """
    prev = session.get("files", {})
    current_posix = {f.relative_to(root).as_posix(): f for f in files}

    modified: list[Path] = []
    added:    list[Path] = []

    for posix, path in current_posix.items():
        if posix not in prev:
            added.append(path)
        elif prev[posix] != hash_file(path):
            modified.append(path)

    removed = [p for p in prev if p not in current_posix]

    return RefreshDiff(modified=modified, added=added, removed=removed)


# ── Tree builder ───────────────────────────────────────────────────────────────
def build_tree(files: list[Path], root: Path, token_map: dict[str, int]) -> str:
    """Indented tree with per-file token counts."""
    tree: dict = {}
    for f in files:
        parts = f.relative_to(root).parts
        node  = tree
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if parts[-1] not in node:
            node[parts[-1]] = None

    lines: list[str] = []

    def walk(node: dict, depth: int = 0, path_parts: tuple = ()) -> None:
        dirs  = sorted(k for k, v in node.items() if isinstance(v, dict))
        leafs = sorted(k for k, v in node.items() if v is None)
        for k in dirs:
            lines.append("  " * depth + k + "/")
            walk(node[k], depth + 1, path_parts + (k,))
        for k in leafs:
            rel_str = "/".join(path_parts + (k,))
            tok     = token_map.get(rel_str, 0)
            suffix  = f"  (~{tok} tokens)" if tok else ""
            lines.append("  " * depth + k + suffix)

    walk(tree)
    return "\n".join(lines)


# ── Summary ────────────────────────────────────────────────────────────────────
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
                    extras = [k for k in ("react", "next", "vue", "typescript", "vite") if k in deps]
                    if extras:
                        label += " / " + " / ".join(e.capitalize() for e in extras)
                except (OSError, json.JSONDecodeError):
                    pass
            return label
    if list(root.glob("*.csproj")):
        return ".NET / C#"
    return "Unknown"


def auto_summary(
    root: Path,
    files: list[Path],
    branch: str,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
) -> str:
    # File count now uses the same filtering pipeline as scanning — consistent stats
    try:
        total = sum(
            1 for p in root.rglob("*")
            if p.is_file()
            and not should_skip(p.relative_to(root), gitignore, exclude_spec, raw_excludes)
        )
    except OSError:
        total = 0

    folders   = {p.parent.name for p in files if p.parent != root}
    arch      = [f"- `{k}/` — {v}" for k, v in ARCH_FOLDERS.items() if k in folders]
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
        *(arch or ["- (architecture not inferred)"]),
    ])


# ── Context builder ────────────────────────────────────────────────────────────
def build_context(
    root: Path,
    result: ScanResult,
    prompt: str,
    refresh_mode: bool,
    session: dict,
    branch: str,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
) -> tuple[str, ContextStats]:
    stats = ContextStats()

    notes = ""
    try:
        notes_path = root / NOTES_FILE
        if notes_path.exists():
            notes = notes_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    # Compute full refresh diff (modified + added + removed)
    diff = compute_refresh_diff(result.files, session, root) if refresh_mode else None
    if diff and diff.total():
        print(
            f"[dgc] Refresh: {len(diff.modified)} modified, "
            f"{len(diff.added)} added, {len(diff.removed)} removed"
        )
    elif diff:
        print("[dgc] Refresh: no changes detected")

    # Pre-read all snippets to populate token_map for the tree
    # This also means each file is read exactly once — no double-reading
    snippets: dict[str, tuple[str, int]] = {}
    for f in result.files:
        rel = f.relative_to(root).as_posix()
        snippets[rel] = read_snippet(f)

    token_map = {rel: tok for rel, (_, tok) in snippets.items()}

    lines: list[str] = [
        auto_summary(root, result.files, branch, gitignore, exclude_spec, raw_excludes),
        "",
    ]

    if notes:
        lines += ["## Session Notes", notes, ""]

    if prompt:
        lines += ["## Session Goal", prompt, ""]

    lines += [
        "## File Tree",
        "```",
        build_tree(result.files, root, token_map),
        "```",
        "",
    ]

    # Removed files section (refresh mode only)
    if diff and diff.removed:
        lines += ["## Removed Files", ""]
        lines += [f"- {p}" for p in diff.removed]
        lines.append("")

    # Track header token cost before adding file content
    header_tokens = count_tokens("\n".join(lines))
    stats.tokens  = header_tokens

    # Determine which files to include content for
    if diff is not None:
        # Refresh: only repack changed files; list unchanged ones
        include_set = set(f.relative_to(root).as_posix() for f in diff.changed)
    else:
        include_set = set(snippets.keys())

    skipped:  list[str] = []
    omitted:  list[str] = []

    for f in result.files:
        rel = f.relative_to(root).as_posix()

        if diff is not None and rel not in include_set:
            skipped.append(rel)
            continue

        snippet, file_tokens = snippets[rel]

        if stats.tokens + file_tokens > MAX_TOTAL_TOKENS:
            omitted.append(rel)
            stats.omitted += 1
            continue

        stats.tokens   += file_tokens
        stats.included += 1
        ext = f.suffix.lstrip(".")
        lines += [f"## {rel}", f"```{ext}", snippet, "```", ""]

    # Omitted files list — more informative than just a count
    if omitted:
        lines += ["## Omitted Files", ""]
        show = omitted[:10]
        lines += [f"- {p}" for p in show]
        if len(omitted) > 10:
            lines.append(f"- (+{len(omitted) - 10} more)")
        lines.append("")

    if skipped:
        lines += ["## Unchanged (omitted from refresh)", ""]
        lines += [f"- {s}" for s in skipped[:20]]
        lines.append("")

    lines += [
        "---",
        f"*dgc.py — {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"edit {NOTES_FILE} for persistent notes*",
    ]

    return "\n".join(lines), stats


# ── Write ──────────────────────────────────────────────────────────────────────
def write_context(
    root: Path,
    result: ScanResult,
    prompt: str,
    refresh_mode: bool,
    session: dict,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
) -> tuple[ContextStats, str]:
    branch = git_branch(root)
    text, stats = build_context(
        root, result, prompt, refresh_mode, session,
        branch, gitignore, exclude_spec, raw_excludes,
    )
    (root / CONTEXT_FILE).write_text(text, encoding="utf-8")
    save_session(root, result.files)
    return stats, branch


# ── Watch mode ─────────────────────────────────────────────────────────────────
def watch_mode(
    root: Path,
    focus: str,
    prompt: str,
    gitignore: object | None,
    exclude_spec: object | None,
    raw_excludes: list[str],
    diverse: bool,
) -> None:
    print("[dgc] Watch mode active. Ctrl+C to stop.")

    def regenerate() -> ScanResult:
        result   = scan_project(root, focus, prompt, gitignore, exclude_spec, raw_excludes, diverse)
        stats, _ = write_context(root, result, prompt, False, {}, gitignore, exclude_spec, raw_excludes)
        print(
            f"[dgc] Regenerated — {stats.included} files, ~{stats.tokens:,} tokens "
            f"({datetime.now().strftime('%H:%M:%S')})"
        )
        return result

    last_result = regenerate()

    if _HAS_WATCHDOG:
        # Watchdog path: file-system events + periodic full rescan to catch new/deleted files.
        # Events alone miss newly created files that weren't in the initial watch set.
        _last_regen = [time.monotonic()]

        class Handler(FileSystemEventHandler):
            def __init__(self) -> None:
                self._last = 0.0

            def on_any_event(self, event) -> None:
                if event.is_directory:
                    return
                now = time.monotonic()
                if now - self._last < 1.5:  # debounce
                    return
                self._last = now
                nonlocal last_result
                last_result = regenerate()
                _last_regen[0] = now

        observer = Observer()
        observer.schedule(Handler(), str(root), recursive=True)
        observer.start()
        print("[dgc] Using watchdog (+ 30s rescan for new/deleted files).")
        try:
            while True:
                time.sleep(1)
                # Periodic full rescan every 30s to catch files watchdog missed
                if time.monotonic() - _last_regen[0] > 30:
                    last_result  = regenerate()
                    _last_regen[0] = time.monotonic()
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    else:
        # Polling fallback: full rescan each cycle so new/deleted/renamed files
        # are always detected — not just modifications to existing files.
        print("[dgc] watchdog not installed — polling every 2s (pip install watchdog).")

        def snapshot() -> tuple[dict[str, str], ScanResult]:
            r = scan_project(root, focus, prompt, gitignore, exclude_spec, raw_excludes, diverse)
            return {f.relative_to(root).as_posix(): hash_file(f) for f in r.files}, r

        last_hashes, last_result = snapshot()

        try:
            while True:
                time.sleep(2)
                current_hashes, current_result = snapshot()
                if current_hashes != last_hashes:
                    last_result  = regenerate()
                    last_hashes, _ = snapshot()
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
    diverse      = "--diverse"        in args
    args = [
        a for a in args
        if a not in ("--context-only", "--launch", "--refresh", "--watch", "--diverse")
    ]

    # --focus <folder>
    focus = ""
    if "--focus" in args:
        i = args.index("--focus")
        if i + 1 < len(args):
            focus = args[i + 1]
            args  = args[:i] + args[i + 2:]
        else:
            print("[dgc] --focus requires a folder argument")
            sys.exit(1)

    # --exclude <pattern>  (repeatable)
    raw_excludes: list[str] = []
    while "--exclude" in args:
        i = args.index("--exclude")
        if i + 1 < len(args):
            raw_excludes.append(args[i + 1])
            args = args[:i] + args[i + 2:]
        else:
            print("[dgc] --exclude requires a pattern argument")
            sys.exit(1)

    prompt = " ".join(args).strip()
    root   = Path.cwd()

    # Compile once — reused for every file during scanning
    gitignore    = load_gitignore(root)
    exclude_spec = compile_excludes(raw_excludes)

    if do_watch:
        watch_mode(root, focus, prompt, gitignore, exclude_spec, raw_excludes, diverse)
        return

    print(f"[dgc] Scanning {root.name}{'/' + focus if focus else ''}...")
    if raw_excludes:
        print(f"[dgc] Excluding: {', '.join(raw_excludes)}")
    if diverse:
        print(f"[dgc] Diversity mode: max {DIVERSE_DIR_CAP} files per directory")

    # Report which optional libs are active
    libs = []
    if _HAS_TIKTOKEN: libs.append("tiktoken")
    if _HAS_PATHSPEC: libs.append("pathspec")
    if _HAS_WATCHDOG: libs.append("watchdog")
    if gitignore:     libs.append(".gitignore")
    if libs:
        print(f"[dgc] Using: {', '.join(libs)}")

    result  = scan_project(root, focus, prompt, gitignore, exclude_spec, raw_excludes, diverse)
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
    stats, branch = write_context(
        root, result, prompt, do_refresh, session,
        gitignore, exclude_spec, raw_excludes,
    )

    size_kb = (root / CONTEXT_FILE).stat().st_size / 1024
    print(f"[dgc] ✓ {CONTEXT_FILE} — {size_kb:.1f} KB, ~{stats.tokens:,} tokens")
    print(f"[dgc] {stats.included} files included, {stats.omitted} omitted")
    print(f"[dgc] Branch: {branch}")

    if not (root / NOTES_FILE).exists():
        print(f"[dgc] Tip: create {NOTES_FILE} for persistent per-project notes")
    else:
        print(f"[dgc] Notes injected from {NOTES_FILE}")

    if context_only:
        print("[dgc] Done. Run 'claude' when ready.")
        return

    if do_launch or choice == "2":
        launch_claude(root, prompt)
    else:
        print(f"[dgc] Done. Run 'claude' in {root.name} when ready.")


if __name__ == "__main__":
    main()
