"""Standalone interactive HTML report generator for codetree.

Produces a single self-contained ``.html`` file (inline CSS/JS, zero external
assets, zero dependencies) that mirrors the GUI experience: summary cards, a
language donut chart, a searchable collapsible file tree and the largest
files. Safe to open from disk, attach to an email or publish as a CI artifact.
"""

import html as _html
import os
import time

from . import __version__
from .core import DirNode, ScanResult, format_size, language_color, sorted_languages

MAX_TREE_NODES = 2500   # cap rendered tree entries so huge repos stay usable
MAX_TOP_FILES = 15

_CSS = """
:root{--bg:#0d1117;--panel:#111826;--card:#161d29;--border:#243041;--fg:#e6edf3;
--muted:#8b949e;--accent:#3b9eff;--code:#3fb950;--comment:#d29922;--blank:#6e7681}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
.top{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end;
padding:26px 30px 18px;border-bottom:1px solid var(--border)}
.top h1{margin:0;font-size:clamp(20px,4vw,30px)}
.sub{margin:4px 0 0;color:var(--muted);font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-left:auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:8px 14px;min-width:92px}
.card b{display:block;font-size:19px}
.card span{color:var(--muted);font-size:12px}
main{padding:22px 30px;max-width:1100px;margin:0 auto}
.row{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px}
.donut-panel{flex:0 0 240px;text-align:center}
.grow{flex:1 1 340px;min-width:0}
h2{margin:0 0 12px;font-size:16px}
.muted{color:var(--muted);font-weight:normal;font-size:13px}
.donut{width:170px;height:170px}
.donut circle{fill:none;stroke-width:7;stroke-linecap:butt}
table{width:100%;border-collapse:collapse;font-size:14px}
.tscroll{overflow-x:auto}
th{color:var(--muted);text-align:left;font-weight:normal;padding:4px 8px;border-bottom:1px solid var(--border)}
td{padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px;vertical-align:-1px}
.bar{height:8px;background:#0a0e14;border-radius:5px;overflow:hidden;min-width:80px}
.bar i{display:block;height:100%;border-radius:5px}
#filter{width:100%;padding:8px 12px;margin:2px 0 14px;border-radius:8px;font-size:14px;
background:var(--card);border:1px solid var(--border);color:var(--fg)}
#filter:focus{outline:1px solid var(--accent)}
#tree{font-size:14px}
#tree details{margin:2px 0}
#tree summary{cursor:pointer;padding:3px 6px;border-radius:6px;list-style:none}
#tree summary::-webkit-details-marker{display:none}
#tree summary:hover,#tree .file:hover{background:var(--card)}
#tree .chev{display:inline-block;width:1.1em;color:var(--muted)}
#tree details[open]>summary .chev::before{content:"▾"}
#tree details:not([open])>summary .chev::before{content:"▸"}
#tree .dname{font-weight:600}
#tree .file{padding:3px 6px 3px 22px;border-radius:6px}
#tree .cnt{color:var(--muted);font-size:12px;margin-left:8px}
#tree .trunc{color:var(--muted);font-style:italic;padding:6px}
.hidden{display:none}
.tf{display:grid;grid-template-columns:minmax(120px,1fr) 70px 140px;gap:10px;
align-items:center;padding:4px 0;font-size:14px}
.tf .path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tf .lines{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}
footer{color:var(--muted);text-align:center;padding:18px;font-size:13px;border-top:1px solid var(--border)}
@media (max-width:640px){.top,main{padding-left:14px;padding-right:14px}
.tf{grid-template-columns:1fr 60px 90px}.donut-panel{flex:1 1 100%}}
"""

_JS = """
(function(){
  var box=document.getElementById('filter');
  if(!box) return;
  box.addEventListener('input',function(){
    var q=box.value.trim().toLowerCase();
    var items=document.querySelectorAll('#tree [data-path]');
    items.forEach(function(el){
      el.classList.toggle('hidden',!!q&&el.dataset.path.toLowerCase().indexOf(q)<0);
    });
    document.querySelectorAll('#tree details').forEach(function(d){
      d.open=q?true:d.hasAttribute('data-open');
    });
    if(!q) return;
    items.forEach(function(el){
      if(el.classList.contains('hidden')) return;
      var p=el.parentElement;
      while(p&&p.id!=='tree'){
        if(p.tagName==='DETAILS'){p.classList.remove('hidden');p.open=true;}
        p=p.parentElement;
      }
    });
  });
})();
"""


def _esc(s) -> str:
    return _html.escape(str(s), quote=True)


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _donut(langs, total: int) -> str:
    # r=15.915 -> circumference ~= 100 units, so dasharray values are percents.
    parts = ['<svg class="donut" viewBox="0 0 42 42" role="img" aria-label="Languages share">',
             '<circle cx="21" cy="21" r="15.915" stroke="#243041"></circle>']
    offset = 25.0
    for l in langs:
        pct = l.total / total * 100
        parts.append(f'<circle cx="21" cy="21" r="15.915" stroke="{language_color(l.name)}" '
                     f'stroke-dasharray="{pct:.3f} {100 - pct:.3f}" stroke-dashoffset="{offset:.3f}"></circle>')
        offset -= pct
    parts.append('<text x="21" y="20" text-anchor="middle" fill="#e6edf3" font-size="5.2" '
                 f'font-weight="600">{total:,}</text>')
    parts.append('<text x="21" y="26" text-anchor="middle" fill="#8b949e" font-size="2.6">lines</text>')
    parts.append("</svg>")
    return "".join(parts)


def _tree(node: DirNode, root: str, budget: list, depth: int = 0) -> str:
    if budget[0] <= 0:
        return '<div class="trunc">… tree truncated (too many entries) — use the search box or the GUI</div>'
    budget[0] -= 1
    rel = _rel(node.path, root)
    out = [f'<details{" open data-open" if depth <= 1 else ""} data-path="{_esc(rel)}">'
           f'<summary><span class="chev"></span><span class="dname">{_esc(node.name)}/</span>'
           f'<span class="cnt">{node.file_count:,} files · {node.total:,} lines</span></summary>']
    for d in node.dirs:
        out.append(_tree(d, root, budget, depth + 1))
    for f in node.files:
        if budget[0] <= 0:
            out.append('<div class="trunc">… tree truncated (too many entries)</div>')
            break
        budget[0] -= 1
        frel = _rel(f.path, root)
        out.append(f'<div class="file" data-path="{_esc(frel)}"><span class="dot" '
                   f'style="background:{language_color(f.language)}"></span>{_esc(f.name)}'
                   f'<span class="cnt">{f.total:,} lines</span></div>')
    out.append("</details>")
    return "".join(out)


def to_html(result: ScanResult, title: str = None) -> str:
    """Render a scan result as one standalone interactive HTML page."""
    r = result.root
    langs = sorted_languages(result)
    total_lines = result.total_lines
    name = title or f"CodeTree report — {r.name or 'project'}"
    top = sorted(result.files, key=lambda f: f.total, reverse=True)[:MAX_TOP_FILES]
    max_lines = top[0].total if top else 0

    cards = "".join(
        f'<div class="card"><b>{v}</b><span>{label}</span></div>' for label, v in (
            ("Files", f"{r.file_count:,}"), ("Folders", f"{result.folder_count:,}"),
            ("Lines", f"{total_lines:,}"), ("Code", f"{r.code:,}"),
            ("Comments", f"{r.comment:,}"), ("Blank", f"{r.blank:,}"),
            ("Size", format_size(r.size)), ("Scan", f"{result.elapsed:.2f}s"),
        ))

    if langs and total_lines:
        lang_rows = []
        for l in langs:
            share = l.total / total_lines
            lang_rows.append(
                f'<tr><td><span class="dot" style="background:{language_color(l.name)}"></span>'
                f'{_esc(l.name)}</td><td class="num">{l.files:,}</td><td class="num">{l.code:,}</td>'
                f'<td class="num">{l.comment:,}</td><td class="num">{l.blank:,}</td>'
                f'<td class="num">{l.total:,}</td><td class="num">{share:.1%}</td>'
                f'<td><div class="bar"><i style="width:{max(share * 100, 0.6):.1f}%;'
                f'background:{language_color(l.name)}"></i></div></td></tr>')
        langs_html = (f'<div class="panel grow"><h2>Languages <span class="muted">'
                      f'({len(langs)} detected)</span></h2>'
                      f'<div class="tscroll"><table><tr><th>Language</th><th class="num">Files</th>'
                      f'<th class="num">Code</th><th class="num">Comments</th><th class="num">Blank</th>'
                      f'<th class="num">Lines</th><th class="num">Share</th><th></th></tr>'
                      f'{"".join(lang_rows)}</table></div></div>')
        donut_html = f'<div class="panel donut-panel">{_donut(langs, total_lines)}</div>'
    else:
        donut_html = ""
        langs_html = '<div class="panel grow"><h2>Languages</h2><p class="muted">No files counted.</p></div>'

    budget = [MAX_TREE_NODES]
    tree_html = _tree(r, r.path, budget)

    top_rows = []
    for f in top:
        rel = _rel(f.path, r.path)
        top_rows.append(
            f'<div class="tf"><span class="path" title="{_esc(rel)}">{_esc(rel)}</span>'
            f'<span class="lines">{f.total:,}</span><div class="bar"><i style="width:'
            f'{(f.total / max_lines * 100) if max_lines else 0:.1f}%;background:'
            f'{language_color(f.language)}"></i></div></div>')
    top_html = ("" if not top_rows else
                f'<section class="panel" style="margin-top:18px"><h2>Largest files</h2>'
                f'{"".join(top_rows)}</section>')

    return (
        f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_esc(name)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n'
        f'<header class="top"><div><h1>🌳 {_esc(r.name or "project")}</h1>'
        f'<p class="sub">CodeTree report · generated {time.strftime("%Y-%m-%d %H:%M")} · '
        f'{_esc("cancelled early" if result.cancelled else f"{result.elapsed:.2f}s scan")}'
        f'</p></div><div class="cards">{cards}</div></header>\n<main>\n'
        f'<section class="row">{donut_html}{langs_html}</section>\n'
        f'<section class="panel"><h2>Files <span class="muted">({r.file_count:,} files, '
        f'{result.folder_count:,} folders)</span></h2>'
        f'<input id="filter" type="search" placeholder="Filter files and folders…" '
        f'aria-label="Filter files and folders">\n<div id="tree">{tree_html}</div></section>\n'
        f'{top_html}\n</main>\n'
        f'<footer>Generated by <a href="https://github.com/rezamardani01/codetree" '
        f'target="_blank" rel="noopener">CodeTree v{_esc(__version__)}</a> — a living tree of your code.</footer>\n'
        f'<script>{_JS}</script>\n</body>\n</html>\n'
    )
