"""Command line entry point. Opens the GUI by default; ``--cli`` prints to the terminal."""

import argparse
import os
import sys

from . import __version__
from .core import (DirNode, ScanOptions, ScanResult, format_size, scan, sorted_languages,
                   to_csv, to_json, to_markdown)

SORT_KEYS = {
    "name": lambda n: n.name.lower(),
    "lines": lambda n: -n.total,
    "code": lambda n: -n.code,
}


class _Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.on else str(text)


def _print_tree(node: DirNode, c: _Style, sort: str, depth: int, prefix: str = "", level: int = 0) -> None:
    key = SORT_KEYS[sort]
    items = [("d", d) for d in sorted(node.dirs, key=key)] + [("f", f) for f in sorted(node.files, key=key)]
    for i, (kind, item) in enumerate(items):
        last = i == len(items) - 1
        branch = "└── " if last else "├── "
        if kind == "d":
            print(f"{prefix}{branch}{c(item.name + '/', '1;34')}  {c(f'{item.total:,} lines', '90')}")
            if depth <= 0 or level + 1 < depth:
                _print_tree(item, c, sort, depth, prefix + ("    " if last else "│   "), level + 1)
        else:
            print(f"{prefix}{branch}{item.name}  {c(f'{item.total:,} lines', '32')}"
                  f" {c('(' + item.language + ')', '90')}")


def _print_report(result: ScanResult, args, c: _Style) -> None:
    print(c(f"\n{result.root.name}/", "1;36"))
    _print_tree(result.root, c, args.sort, args.depth)

    langs = sorted_languages(result)
    if langs:
        print(c("\nLanguages", "1"))
        width = max(len(l.name) for l in langs)
        total = result.total_lines or 1
        print(f"  {'Language'.ljust(width)}  {'Files':>6} {'Code':>9} {'Comment':>9} {'Blank':>9} {'Share':>7}")
        for l in langs:
            bar = "█" * max(1, round(l.total / total * 20))
            print(f"  {l.name.ljust(width)}  {l.files:>6} {l.code:>9,} {l.comment:>9,} {l.blank:>9,} "
                  f"{l.total / total:>6.1%} {c(bar, '36')}")

    if args.top:
        print(c(f"\nTop {args.top} largest files", "1"))
        for f in sorted(result.files, key=lambda f: f.total, reverse=True)[: args.top]:
            print(f"  {f.total:>8,}  {os.path.relpath(f.path, result.root.path)}")

    r = result.root
    print(c("\nSummary", "1"))
    print(f"  Files:    {r.file_count:,}")
    print(f"  Folders:  {result.folder_count:,}")
    print(f"  Lines:    {r.total:,}  (code {r.code:,} · comments {r.comment:,} · blank {r.blank:,})")
    print(f"  Size:     {format_size(r.size)}")
    print(f"  Time:     {result.elapsed:.2f}s")
    if result.skipped_binary or result.skipped_large:
        print(c(f"  Skipped:  {result.skipped_binary} binary, {result.skipped_large} too large", "90"))
    for err in result.errors[:5]:
        print(c(f"  ! {err}", "33"), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codetree",
        description="Analyze a project's lines of code. Opens a graphical window by default.")
    p.add_argument("path", nargs="?", default=".", help="project directory (default: current directory)")
    p.add_argument("--cli", action="store_true", help="print the report in the terminal instead of opening the GUI")
    p.add_argument("--json", metavar="FILE", help="write a JSON report ('-' for stdout); implies --cli")
    p.add_argument("--csv", metavar="FILE", help="write a CSV report ('-' for stdout); implies --cli")
    p.add_argument("--md", metavar="FILE", help="write a Markdown report ('-' for stdout); implies --cli")
    p.add_argument("-i", "--ignore", action="append", default=[], metavar="PATTERN",
                   help="glob pattern to ignore (repeatable), e.g. -i '*.min.js' -i tests")
    p.add_argument("-e", "--ext", action="append", default=[], metavar="EXT",
                   help="only count these extensions (repeatable), e.g. -e py -e js")
    p.add_argument("--hidden", action="store_true", help="include hidden files and folders")
    p.add_argument("--no-gitignore", action="store_true", help="do not read .gitignore")
    p.add_argument("--no-default-ignores", action="store_true", help="do not skip node_modules, venv, build, ...")
    p.add_argument("--max-size", type=float, default=5, metavar="MB", help="skip files larger than this (default 5)")
    p.add_argument("--sort", choices=sorted(SORT_KEYS), default="name", help="tree sort order (CLI)")
    p.add_argument("--depth", type=int, default=0, help="limit tree depth, 0 = unlimited (CLI)")
    p.add_argument("--top", type=int, default=0, metavar="N", help="list the N largest files (CLI)")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    p.add_argument("-V", "--version", action="version", version=f"codetree {__version__}")
    return p


def _emit(target: str, text: str) -> None:
    if target == "-":
        sys.stdout.write(text)
    else:
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print(f"Saved {target}", file=sys.stderr)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"codetree: '{args.path}' is not a directory.", file=sys.stderr)
        return 2

    options = ScanOptions(
        include_hidden=args.hidden,
        use_default_ignores=not args.no_default_ignores,
        respect_gitignore=not args.no_gitignore,
        ignore=args.ignore,
        extensions=args.ext,
        max_size=int(args.max_size * 1024 * 1024),
    )

    exports = [(args.json, to_json), (args.csv, to_csv), (args.md, to_markdown)]
    wants_cli = args.cli or any(t for t, _ in exports)

    if not wants_cli:
        try:
            from .gui import run_gui
        except ImportError as e:
            print(f"codetree: GUI unavailable ({e}); falling back to terminal output.", file=sys.stderr)
        else:
            try:
                run_gui(path, options)
                return 0
            except Exception as e:  # e.g. no display
                print(f"codetree: could not open the GUI ({e}); falling back to terminal output.", file=sys.stderr)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = scan(path, options)
    except KeyboardInterrupt:
        return 130
    stdout_taken = any(t == "-" for t, _ in exports)
    for target, fn in exports:
        if target:
            _emit(target, fn(result))
    if not stdout_taken:
        color = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if color and os.name == "nt":
            os.system("")  # enable ANSI escapes on Windows consoles
        _print_report(result, args, _Style(color))
    return 0


def main_gui() -> int:
    """Entry point for the ``codetree-gui`` launcher (no console window on Windows)."""
    return main(sys.argv[1:] if len(sys.argv) > 1 else ["."])


if __name__ == "__main__":
    sys.exit(main())
