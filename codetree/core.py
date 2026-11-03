"""Scanning engine for codetree: walks a project and classifies every line."""

import csv
import fnmatch
import io
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# (name, extensions, line comment prefixes, block comment (start, end) pairs, color)
_C_LIKE = (("//",), (("/*", "*/"),))
_HASH = (("#",), ())
_LANGS = [
    ("Python", (".py", ".pyw", ".pyi"), ("#",), (('"""', '"""'), ("'''", "'''")), "#3572A5"),
    ("JavaScript", (".js", ".mjs", ".cjs", ".jsx"), *_C_LIKE, "#f1e05a"),
    ("TypeScript", (".ts", ".tsx"), *_C_LIKE, "#3178c6"),
    ("HTML", (".html", ".htm"), (), (("<!--", "-->"),), "#e34c26"),
    ("CSS", (".css",), (), (("/*", "*/"),), "#563d7c"),
    ("SCSS/Sass", (".scss", ".sass", ".less"), *_C_LIKE, "#c6538c"),
    ("Vue", (".vue",), (), (("<!--", "-->"),), "#41b883"),
    ("Svelte", (".svelte",), (), (("<!--", "-->"),), "#ff3e00"),
    ("Java", (".java",), *_C_LIKE, "#b07219"),
    ("Kotlin", (".kt", ".kts"), *_C_LIKE, "#A97BFF"),
    ("C", (".c",), *_C_LIKE, "#8a8a8a"),
    ("C Header", (".h",), *_C_LIKE, "#a0a0c0"),
    ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), *_C_LIKE, "#f34b7d"),
    ("C#", (".cs",), *_C_LIKE, "#178600"),
    ("Go", (".go",), *_C_LIKE, "#00ADD8"),
    ("Rust", (".rs",), *_C_LIKE, "#dea584"),
    ("Swift", (".swift",), *_C_LIKE, "#F05138"),
    ("Dart", (".dart",), *_C_LIKE, "#00B4AB"),
    ("PHP", (".php",), ("//", "#"), (("/*", "*/"),), "#4F5D95"),
    ("Ruby", (".rb",), ("#",), (("=begin", "=end"),), "#701516"),
    ("Lua", (".lua",), ("--",), (("--[[", "]]"),), "#000080"),
    ("Perl", (".pl", ".pm"), ("#",), (), "#0298c3"),
    ("R", (".r",), ("#",), (), "#198CE7"),
    ("Scala", (".scala",), *_C_LIKE, "#c22d40"),
    ("Shell", (".sh", ".bash", ".zsh"), ("#",), (), "#89e051"),
    ("PowerShell", (".ps1", ".psm1"), ("#",), (("<#", "#>"),), "#012456"),
    ("Batch", (".bat", ".cmd"), ("REM ", "::"), (), "#C1F12E"),
    ("SQL", (".sql",), ("--",), (("/*", "*/"),), "#e38c00"),
    ("JSON", (".json", ".jsonc"), (), (), "#a8a8a8"),
    ("YAML", (".yml", ".yaml"), ("#",), (), "#cb171e"),
    ("TOML", (".toml",), ("#",), (), "#9c4221"),
    ("INI/Config", (".ini", ".cfg", ".conf", ".env"), ("#", ";"), (), "#8a8a8a"),
    ("XML", (".xml", ".xaml", ".svg"), (), (("<!--", "-->"),), "#0060ac"),
    ("Markdown", (".md", ".markdown"), (), (("<!--", "-->"),), "#083fa1"),
    ("Text", (".txt", ".rst"), (), (), "#9aa0a6"),
    ("CSV", (".csv", ".tsv"), (), (), "#237346"),
    ("Dockerfile", (".dockerfile",), ("#",), (), "#384d54"),
    ("Makefile", (".mk",), ("#",), (), "#427819"),
    ("Haskell", (".hs",), ("--",), (("{-", "-}"),), "#5e5086"),
    ("Elixir", (".ex", ".exs"), ("#",), (), "#6e4a7e"),
    ("Objective-C", (".m", ".mm"), *_C_LIKE, "#438eff"),
    ("Assembly", (".asm", ".s"), (";",), (), "#6E4C13"),
]
_FILENAMES = {"dockerfile": "Dockerfile", "makefile": "Makefile", "cmakelists.txt": "Makefile"}

LANG_SPECS: Dict[str, Tuple[tuple, tuple, str]] = {}
EXT_TO_LANG: Dict[str, str] = {}
for _name, _exts, _lc, _bc, _color in _LANGS:
    LANG_SPECS[_name] = (_lc, _bc, _color)
    for _e in _exts:
        EXT_TO_LANG[_e] = _name
LANG_SPECS["Other"] = ((), (), "#6e7681")

DEFAULT_IGNORE_DIRS = {
    "node_modules", "__pycache__", "venv", "env", ".venv", "dist", "build", "target",
    ".git", ".hg", ".svn", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".tox",
    ".next", ".nuxt", ".gradle", ".dart_tool", "bower_components", "site-packages",
    "coverage", ".cache",
}
DEFAULT_IGNORE_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
                        "Cargo.lock", "composer.lock", ".DS_Store", "Thumbs.db"}


def detect_language(filename: str) -> str:
    low = filename.lower()
    if low in _FILENAMES:
        return _FILENAMES[low]
    ext = os.path.splitext(low)[1]
    return EXT_TO_LANG.get(ext, "Other")


@dataclass
class ScanOptions:
    include_hidden: bool = False
    use_default_ignores: bool = True
    respect_gitignore: bool = True
    ignore: List[str] = field(default_factory=list)      # fnmatch patterns
    extensions: List[str] = field(default_factory=list)  # e.g. [".py", ".js"]; empty = all
    max_size: int = 5 * 1024 * 1024                      # skip bigger files (bytes)


@dataclass
class FileStat:
    path: str
    name: str
    language: str
    code: int
    comment: int
    blank: int
    size: int

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank


@dataclass
class DirNode:
    name: str
    path: str
    dirs: List["DirNode"] = field(default_factory=list)
    files: List[FileStat] = field(default_factory=list)
    file_count: int = 0
    code: int = 0
    comment: int = 0
    blank: int = 0
    size: int = 0

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank

    def finalize(self) -> None:
        for d in self.dirs:
            d.finalize()
        self.dirs = [d for d in self.dirs if d.file_count]  # drop folders with nothing countable
        self.dirs.sort(key=lambda d: d.name.lower())
        self.files.sort(key=lambda f: f.name.lower())
        self.file_count = len(self.files)
        self.code = sum(f.code for f in self.files)
        self.comment = sum(f.comment for f in self.files)
        self.blank = sum(f.blank for f in self.files)
        self.size = sum(f.size for f in self.files)
        for d in self.dirs:
            self.file_count += d.file_count
            self.code += d.code
            self.comment += d.comment
            self.blank += d.blank
            self.size += d.size


@dataclass
class LangStat:
    name: str
    files: int = 0
    code: int = 0
    comment: int = 0
    blank: int = 0

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank


@dataclass
class ScanResult:
    root: DirNode
    languages: Dict[str, LangStat]
    files: List[FileStat]
    folder_count: int = 0
    skipped_binary: int = 0
    skipped_large: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    cancelled: bool = False

    @property
    def total_files(self) -> int:
        return self.root.file_count

    @property
    def total_lines(self) -> int:
        return self.root.total


def classify_lines(lines, line_comments, blocks) -> Tuple[int, int, int]:
    """Return (code, comment, blank) for an iterable of text lines."""
    code = comment = blank = 0
    in_block = None
    for raw in lines:
        s = raw.strip()
        if in_block:
            comment += 1
            if in_block in s:
                in_block = None
            continue
        if not s:
            blank += 1
            continue
        if line_comments and s.startswith(line_comments):
            comment += 1
            continue
        opened = False
        for start, end in blocks:
            if s.startswith(start):
                if end not in s[len(start):]:
                    in_block = end
                comment += 1
                opened = True
                break
        if opened:
            continue
        code += 1
        for start, end in blocks:  # code followed by an unclosed block comment
            if start != end:
                i = s.find(start)
                if i > 0 and end not in s[i + len(start):]:
                    in_block = end
                    break
    return code, comment, blank


def _is_binary(path: str) -> bool:
    with open(path, "rb") as fh:
        return b"\0" in fh.read(8192)


def analyze_file(path: str) -> Optional[FileStat]:
    """Analyze a single text file; returns None for binary files."""
    if _is_binary(path):
        return None
    name = os.path.basename(path)
    lang = detect_language(name)
    lc, bc, _ = LANG_SPECS[lang]
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        code, comment, blank = classify_lines(fh, tuple(lc), bc)
    return FileStat(path, name, lang, code, comment, blank, os.path.getsize(path))


def _load_gitignore(directory: str) -> List[str]:
    patterns = []
    try:
        with open(os.path.join(directory, ".gitignore"), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith(("#", "!")):
                    patterns.append(line.rstrip("/").lstrip("/"))
    except OSError:
        pass
    return patterns


def scan(path: str, options: Optional[ScanOptions] = None,
         progress: Optional[Callable[[int, str], None]] = None,
         cancel: Optional[Callable[[], bool]] = None) -> ScanResult:
    """Scan ``path`` and return a :class:`ScanResult`.

    ``progress(files_done, current_path)`` is called while scanning and
    ``cancel()`` may return True to stop early.
    """
    opts = options or ScanOptions()
    root_path = os.path.abspath(path)
    started = time.perf_counter()
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in opts.extensions}
    user_ignore = list(opts.ignore)
    git_patterns = _load_gitignore(root_path) if opts.respect_gitignore else []

    all_files: List[FileStat] = []
    result = ScanResult(DirNode(os.path.basename(root_path) or root_path, root_path), {}, all_files)

    def ignored(name: str, rel: str, is_dir: bool) -> bool:
        if name.startswith(".") and not opts.include_hidden:
            return True
        if opts.use_default_ignores:
            if is_dir and name in DEFAULT_IGNORE_DIRS:
                return True
            if not is_dir and name in DEFAULT_IGNORE_FILES:
                return True
        elif is_dir and name in (".git", ".hg", ".svn"):
            return True
        rel = rel.replace(os.sep, "/")
        for pat in user_ignore + git_patterns:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    def walk(node: DirNode, rel_dir: str) -> None:
        if cancel and cancel():
            result.cancelled = True
            return
        try:
            entries = sorted(os.scandir(node.path), key=lambda e: e.name.lower())
        except OSError as e:
            result.errors.append(f"{node.path}: {e.strerror or e}")
            return
        for entry in entries:
            if result.cancelled:
                return
            rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file()
            except OSError:
                continue
            if not (is_dir or is_file) or ignored(entry.name, rel, is_dir):
                continue
            if is_dir:
                child = DirNode(entry.name, entry.path)
                node.dirs.append(child)
                walk(child, rel)
                continue
            if exts and os.path.splitext(entry.name)[1].lower() not in exts:
                continue
            try:
                if entry.stat().st_size > opts.max_size:
                    result.skipped_large += 1
                    continue
                stat = analyze_file(entry.path)
            except OSError as e:
                result.errors.append(f"{entry.path}: {e.strerror or e}")
                continue
            if stat is None:
                result.skipped_binary += 1
                continue
            node.files.append(stat)
            all_files.append(stat)
            if progress:
                progress(len(all_files), entry.path)

    walk(result.root, "")
    result.root.finalize()

    def count_dirs(n: DirNode) -> int:
        return 1 + sum(count_dirs(d) for d in n.dirs)

    result.folder_count = count_dirs(result.root)
    for f in all_files:
        ls = result.languages.setdefault(f.language, LangStat(f.language))
        ls.files += 1
        ls.code += f.code
        ls.comment += f.comment
        ls.blank += f.blank
    result.elapsed = time.perf_counter() - started
    return result


# ---------------------------------------------------------------- exporting

def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def language_color(name: str) -> str:
    return LANG_SPECS.get(name, LANG_SPECS["Other"])[2]


def sorted_languages(result: ScanResult) -> List[LangStat]:
    return sorted(result.languages.values(), key=lambda l: l.total, reverse=True)


def to_dict(result: ScanResult) -> dict:
    root = result.root.path
    return {
        "path": root,
        "summary": {
            "files": result.total_files, "folders": result.folder_count,
            "lines": result.total_lines, "code": result.root.code,
            "comments": result.root.comment, "blank": result.root.blank,
            "bytes": result.root.size, "seconds": round(result.elapsed, 3),
            "skipped_binary": result.skipped_binary, "skipped_large": result.skipped_large,
        },
        "languages": [
            {"language": l.name, "files": l.files, "code": l.code, "comments": l.comment,
             "blank": l.blank, "lines": l.total} for l in sorted_languages(result)],
        "files": [
            {"path": os.path.relpath(f.path, root).replace(os.sep, "/"), "language": f.language,
             "code": f.code, "comments": f.comment, "blank": f.blank, "lines": f.total,
             "bytes": f.size} for f in sorted(result.files, key=lambda f: f.path)],
    }


def to_json(result: ScanResult) -> str:
    return json.dumps(to_dict(result), indent=2, ensure_ascii=False)


def to_csv(result: ScanResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["path", "language", "code", "comments", "blank", "lines", "bytes"])
    for f in to_dict(result)["files"]:
        w.writerow([f["path"], f["language"], f["code"], f["comments"], f["blank"], f["lines"], f["bytes"]])
    return buf.getvalue()


def to_markdown(result: ScanResult) -> str:
    d = to_dict(result)
    s = d["summary"]
    out = [f"# CodeTree report: `{os.path.basename(d['path']) or d['path']}`", "",
           f"- **Files:** {s['files']}  \n- **Folders:** {s['folders']}  \n"
           f"- **Total lines:** {s['lines']} (code {s['code']}, comments {s['comments']}, blank {s['blank']})  \n"
           f"- **Size:** {format_size(s['bytes'])}", "",
           "## Languages", "", "| Language | Files | Code | Comments | Blank | Total |",
           "|---|---:|---:|---:|---:|---:|"]
    for l in d["languages"]:
        out.append(f"| {l['language']} | {l['files']} | {l['code']} | {l['comments']} | {l['blank']} | {l['lines']} |")
    out += ["", "## Largest files", "", "| File | Lines |", "|---|---:|"]
    for f in sorted(d["files"], key=lambda f: f["lines"], reverse=True)[:15]:
        out.append(f"| `{f['path']}` | {f['lines']} |")
    return "\n".join(out) + "\n"
