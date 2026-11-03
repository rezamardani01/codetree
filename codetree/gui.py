"""CodeTree graphical interface: an animated, living tree of your project (Tkinter only)."""

import json
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (DirNode, FileStat, ScanOptions, ScanResult, format_size, language_color, scan,
                   sorted_languages, to_csv, to_json, to_markdown)

THEMES = {
    "dark": dict(bg="#0a0e14", panel="#111720", card="#161d29", border="#243041", fg="#e6edf3",
                 muted="#8b949e", accent="#3b9eff", accent_fg="#ffffff", select="#1c2f4a",
                 code="#3fb950", comment="#d29922", blank="#6e7681", glow="#bfe9a8",
                 branch=["#a9824f", "#7dae5a", "#4cc38a", "#38bdd0", "#58a6ff", "#a78bfa"]),
    "light": dict(bg="#eef3f8", panel="#ffffff", card="#ffffff", border="#d0d7de", fg="#1f2328",
                  muted="#656d76", accent="#0969da", accent_fg="#ffffff", select="#ddf4ff",
                  code="#1a7f37", comment="#9a6700", blank="#8c959f", glow="#7cc36a",
                  branch=["#8a6a3c", "#4f8f3a", "#2f9d6f", "#1b97a8", "#2b7fd6", "#7c5ce6"]),
}
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".codetree.json")
FILE_CAP = 90          # leaves shown per folder before a "+N more" node
GROW_CAP = 110         # nodes shown by the automatic growth animation
EXPAND_ALL_CAP = 400
SEARCH_CAP = 300       # nodes shown while searching


@lru_cache(maxsize=4096)
def mix(c1: str, c2: str, a: float) -> str:
    """Blend c1 over c2 with weight a (Tk canvas has no alpha channel)."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return "#%02x%02x%02x" % (round(r2 + (r1 - r2) * a), round(g2 + (g1 - g2) * a), round(b2 + (b1 - b2) * a))


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def _open_path(path: str, reveal: bool = False) -> None:
    try:
        if sys.platform.startswith("win"):
            if reveal:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])
    except OSError as e:
        messagebox.showerror("CodeTree", f"Could not open:\n{path}\n\n{e}")


class VNode:
    """One animated node of the visual tree (folder, file or '+N more')."""
    __slots__ = ("key", "kind", "data", "name", "parent", "children", "depth", "x", "y", "tx", "ty", "g",
                 "delay", "dying", "expanded", "r", "ew", "total", "lang", "e", "gl", "o", "t", "b",
                 "cstate", "lab_vis", "shown", "bcount", "anc", "ewc")

    def __init__(self, key, kind):
        self.key, self.kind = key, kind
        self.parent = None
        self.children = []
        self.x = self.y = self.tx = self.ty = 0.0
        self.g = 0.0
        self.delay = 0.0
        self.dying = False
        self.expanded = False
        self.depth = 0
        self.e = self.gl = self.o = self.t = self.b = None
        self.cstate = None
        self.lab_vis = None
        self.shown = None
        self.lang = None
        self.bcount = 0
        self.anc = None
        self.ewc = None


class App:
    def __init__(self, root: tk.Tk, path: str, options: ScanOptions):
        self.root = root
        self.cfg = _load_config()
        self.theme_name = self.cfg.get("theme", "dark")
        self.t = THEMES[self.theme_name]
        self.result = None  # type: ScanResult
        self.queue = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.scanning = False
        self.scan_count = 0
        self.root_node = None

        # tree state
        self.vnodes = {}
        self.expanded = set()
        self.show_all = set()
        self.selected = None
        self.hover = None
        self.hot = set()
        self.hl_lang = None
        self.query = ""
        self.cam = [1.0, 0.0, 0.0]
        self.cam_t = [1.0, 0.0, 0.0]
        self.view_dirty = True
        self.cur_fs = 10
        self.last_t = time.perf_counter()
        self.grow_token = 0
        self.fonts = {}
        self.press = None
        self.last_click = (None, 0.0)
        self.fireflies = []
        self.scan_items = []
        self.lang_rows = []
        self.top_rows = []

        root.title(f"CodeTree {__version__}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1380, int(sw * 0.92)), min(860, int(sh * 0.88))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        root.minsize(min(1000, w), min(580, h))

        family = "Segoe UI" if sys.platform.startswith("win") else tkfont.nametofont("TkDefaultFont").actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=10)
        self.family = family
        self.f_title = tkfont.Font(family=family, size=20, weight="bold")
        self.f_big = tkfont.Font(family=family, size=15, weight="bold")
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_bold = tkfont.Font(family=family, size=10, weight="bold")

        self.var_path = tk.StringVar(value=path)
        self.var_hidden = tk.BooleanVar(value=options.include_hidden)
        self.var_git = tk.BooleanVar(value=options.respect_gitignore)
        self.var_defign = tk.BooleanVar(value=options.use_default_ignores)
        self.var_ignore = tk.StringVar(value=", ".join(options.ignore))
        self.var_ext = tk.StringVar(value=", ".join(options.extensions))
        self.var_search = tk.StringVar()
        self.var_status = tk.StringVar(value="Ready")
        self.cards = {}

        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()
        self._bind()
        self.apply_theme()

        self.path_box.configure(values=self.cfg.get("recent", []))
        root.after(30, self.tick)
        root.after(80, self.poll)
        root.after(200, self.start_scan)

    # ================================================================== UI
    def _build(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(20, 14, 20, 8))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer, style="App.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="🌳 CodeTree", style="Title.TLabel", font=self.f_title).pack(side="left")
        ttk.Label(head, text=f"  v{__version__}  ·  the tree of your code", style="Muted.TLabel").pack(side="left", pady=(8, 0))
        ttk.Button(head, text="◐ Theme", style="Ghost.TButton", command=self.toggle_theme).pack(side="right")
        self.btn_export = ttk.Menubutton(head, text="⭳ Export", style="Ghost.TMenubutton")
        self.export_menu = tk.Menu(self.btn_export, tearoff=0)
        for label, kind in (("JSON…", "json"), ("CSV…", "csv"), ("Markdown…", "md")):
            self.export_menu.add_command(label=label, command=lambda k=kind: self.export(k))
        self.btn_export["menu"] = self.export_menu
        self.btn_export.pack(side="right", padx=8)

        bar = ttk.Frame(outer, style="Panel.TFrame", padding=12)
        bar.pack(fill="x", pady=(14, 0))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Project", style="Panel.TLabel", font=self.f_bold).grid(row=0, column=0, padx=(0, 10))
        self.path_box = ttk.Combobox(bar, textvariable=self.var_path)
        self.path_box.grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="Browse…", style="Ghost.TButton", command=self.browse).grid(row=0, column=2, padx=8)
        self.btn_scan = ttk.Button(bar, text="▶  Scan", style="Accent.TButton", command=self.start_scan)
        self.btn_scan.grid(row=0, column=3)
        self.btn_cancel = ttk.Button(bar, text="■ Stop", style="Ghost.TButton", command=self.cancel, state="disabled")
        self.btn_cancel.grid(row=0, column=4, padx=(8, 0))

        opts = ttk.Frame(bar, style="Panel.TFrame")
        opts.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        for text, var in (("Hidden files", self.var_hidden), (".gitignore", self.var_git),
                          ("Skip node_modules/venv/build…", self.var_defign)):
            ttk.Checkbutton(opts, text=text, variable=var, style="Panel.TCheckbutton").pack(side="left", padx=(0, 16))
        ttk.Label(opts, text="Ignore", style="Panel.TLabel").pack(side="left")
        ttk.Entry(opts, textvariable=self.var_ignore, width=22).pack(side="left", padx=(6, 16))
        ttk.Label(opts, text="Only ext.", style="Panel.TLabel").pack(side="left")
        ttk.Entry(opts, textvariable=self.var_ext, width=14).pack(side="left", padx=6)
        ttk.Label(opts, text="comma-separated: *.min.js, tests  |  py, js", style="PanelMuted.TLabel").pack(side="left", padx=6)

        body = ttk.Frame(outer, style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        tools = ttk.Frame(body, style="App.TFrame")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(tools, text="🔍", style="Muted.TLabel").pack(side="left")
        self.search = ttk.Entry(tools, textvariable=self.var_search, width=26)
        self.search.pack(side="left", padx=(4, 12))
        for text, cmd in (("Expand all", self.expand_all), ("Collapse", self.collapse_all), ("Fit view", self.fit)):
            ttk.Button(tools, text=text, style="Ghost.TButton", command=cmd).pack(side="left", padx=(0, 6))
        ttk.Label(tools, text="click a folder to grow it · drag to pan · wheel to zoom · double-click a file to open",
                  style="Muted.TLabel", font=self.f_small).pack(side="right")

        self.cv = tk.Canvas(body, highlightthickness=0, cursor="arrow")
        self.cv.grid(row=1, column=0, sticky="nsew")

        side = ttk.Frame(body, style="Panel.TFrame", padding=8, width=340)
        side.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(10, 0))
        side.grid_propagate(False)
        cards = ttk.Frame(side, style="Panel.TFrame")
        cards.pack(fill="x", pady=(0, 8))
        cards.columnconfigure(0, weight=1, uniform="c")
        cards.columnconfigure(1, weight=1, uniform="c")
        specs = (("files", "FILES", None), ("folders", "FOLDERS", None), ("total", "TOTAL LINES", None),
                 ("size", "SIZE", None), ("code", "CODE", "code"), ("comment", "COMMENTS", "comment"),
                 ("blank", "BLANK", "blank"), ("time", "SCAN TIME", None))
        self.card_bars = []
        for i, (key, label, color) in enumerate(specs):
            f = ttk.Frame(cards, style="Card.TFrame", padding=(10, 5))
            f.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 6, 0), pady=(0 if i < 2 else 6, 0))
            ttk.Label(f, text=label, style="CardMuted.TLabel", font=self.f_small).pack(anchor="w")
            v = ttk.Label(f, text="–", style="CardValue.TLabel", font=self.f_big)
            v.pack(anchor="w")
            if color:
                bar_w = tk.Frame(f, height=3)
                bar_w.pack(fill="x", pady=(2, 0))
                self.card_bars.append((bar_w, color))
            self.cards[key] = v

        self.nb = ttk.Notebook(side)
        self.nb.pack(fill="both", expand=True)
        self.cv_lang = tk.Canvas(self.nb, highlightthickness=0)
        self.cv_top = tk.Canvas(self.nb, highlightthickness=0)
        self.nb.add(self.cv_lang, text="  Languages  ")
        self.nb.add(self.cv_top, text="  Largest files  ")
        self.cv_lang.bind("<Configure>", lambda e: self.draw_languages())
        self.cv_top.bind("<Configure>", lambda e: self.draw_top())
        self.cv_lang.bind("<Button-1>", self.on_lang_click)
        self.cv_top.bind("<Button-1>", self.on_top_click)

        # info card floating over the canvas
        self.card = tk.Frame(self.cv, bd=0, highlightthickness=1)
        self.card_title = tk.Label(self.card, anchor="w", justify="left")
        self.card_sub = tk.Label(self.card, anchor="w", justify="left")
        self.card_stats = tk.Label(self.card, anchor="w", justify="left")
        self.card_title.pack(fill="x", padx=14, pady=(10, 0))
        self.card_sub.pack(fill="x", padx=14)
        self.card_stats.pack(fill="x", padx=14, pady=(6, 10))

        status = ttk.Frame(outer, style="App.TFrame")
        status.pack(fill="x", pady=(8, 0))
        ttk.Label(status, textvariable=self.var_status, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160, style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="right")

    def _bind(self) -> None:
        r, cv = self.root, self.cv
        r.bind("<F5>", lambda e: self.start_scan())
        r.bind("<Control-o>", lambda e: self.browse())
        r.bind("<Control-f>", lambda e: (self.search.focus_set(), self.search.select_range(0, "end")))
        r.bind("<Control-e>", lambda e: self.export("json"))
        r.bind("<Escape>", lambda e: (self.var_search.set(""), self.select(None)))
        self.path_box.bind("<Return>", lambda e: self.start_scan())
        self.path_box.bind("<<ComboboxSelected>>", lambda e: self.start_scan())
        self.var_search.trace_add("write", lambda *a: self.on_search())
        cv.bind("<Configure>", lambda e: self.mark_dirty())
        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_drag)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Motion>", self.on_motion)
        cv.bind("<Leave>", lambda e: self.set_hover(None))
        cv.bind("<Button-3>", self.on_context)
        cv.bind("<MouseWheel>", lambda e: self.zoom_at(e.x, e.y, 1.12 if e.delta > 0 else 1 / 1.12))
        cv.bind("<Button-4>", lambda e: self.zoom_at(e.x, e.y, 1.12))
        cv.bind("<Button-5>", lambda e: self.zoom_at(e.x, e.y, 1 / 1.12))
        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Open", command=lambda: self.ctx_act("open"))
        self.ctx.add_command(label="Show in folder", command=lambda: self.ctx_act("reveal"))
        self.ctx.add_command(label="Copy path", command=lambda: self.ctx_act("copy"))
        r.protocol("WM_DELETE_WINDOW", self.on_close)

    # ============================================================== theming
    def apply_theme(self) -> None:
        t, s = self.t, self.style
        self.root.configure(bg=t["bg"])
        s.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"],
                    lightcolor=t["border"], darkcolor=t["border"], troughcolor=t["panel"], focuscolor=t["accent"])
        s.configure("App.TFrame", background=t["bg"])
        s.configure("Panel.TFrame", background=t["panel"], bordercolor=t["border"], relief="solid", borderwidth=1)
        s.configure("Card.TFrame", background=t["card"], bordercolor=t["border"], relief="solid", borderwidth=1)
        s.configure("Title.TLabel", background=t["bg"], foreground=t["fg"])
        s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
        s.configure("Panel.TLabel", background=t["panel"], foreground=t["fg"])
        s.configure("PanelMuted.TLabel", background=t["panel"], foreground=t["muted"])
        s.configure("CardValue.TLabel", background=t["card"], foreground=t["fg"])
        s.configure("CardMuted.TLabel", background=t["card"], foreground=t["muted"])
        s.configure("TEntry", fieldbackground=t["bg"], foreground=t["fg"], insertcolor=t["fg"], padding=5)
        s.configure("TCombobox", fieldbackground=t["bg"], background=t["panel"], foreground=t["fg"],
                    arrowcolor=t["fg"], insertcolor=t["fg"], padding=5)
        s.map("TCombobox", fieldbackground=[("readonly", t["bg"])], foreground=[("readonly", t["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", t["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        s.configure("Panel.TCheckbutton", background=t["panel"], foreground=t["fg"], indicatorbackground=t["bg"],
                    indicatorforeground=t["accent"])
        s.map("Panel.TCheckbutton", background=[("active", t["panel"])], indicatorbackground=[("selected", t["accent"])])
        s.configure("Accent.TButton", background=t["accent"], foreground=t["accent_fg"], padding=(16, 6),
                    borderwidth=0, font=self.f_bold)
        s.map("Accent.TButton", background=[("active", t["accent"]), ("disabled", t["border"])],
              foreground=[("disabled", t["muted"])])
        for n in ("Ghost.TButton", "Ghost.TMenubutton"):
            s.configure(n, background=t["panel"], foreground=t["fg"], padding=(12, 5), bordercolor=t["border"])
            s.map(n, background=[("active", t["select"])], foreground=[("disabled", t["muted"])])
        s.configure("TNotebook", background=t["panel"], borderwidth=0)
        s.configure("TNotebook.Tab", background=t["panel"], foreground=t["muted"], padding=(8, 6), borderwidth=0)
        s.map("TNotebook.Tab", foreground=[("selected", t["accent"])], background=[("selected", t["panel"])])
        s.configure("Accent.Horizontal.TProgressbar", background=t["accent"], troughcolor=t["panel"], borderwidth=0)
        self.cv.configure(bg=t["bg"])
        for c in (self.cv_lang, self.cv_top):
            c.configure(bg=t["panel"])
        for m in (self.ctx, self.export_menu):
            m.configure(bg=t["panel"], fg=t["fg"], activebackground=t["accent"], activeforeground="#fff")
        for w, key in self.card_bars:
            w.configure(bg=t[key])
        self.card.configure(bg=t["panel"], highlightbackground=t["border"], highlightcolor=t["border"])
        for lbl, color, font in ((self.card_title, t["fg"], self.f_bold), (self.card_sub, t["muted"], self.f_small),
                                 (self.card_stats, t["fg"], self.f_small)):
            lbl.configure(bg=t["panel"], fg=color, font=font)
        for n in self.vnodes.values():
            n.cstate = None
        for ff in self.fireflies:
            self.cv.delete(ff["id"])
        self.fireflies = []
        self.draw_languages()
        self.draw_top()
        self.mark_dirty()

    def toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.t = THEMES[self.theme_name]
        self.apply_theme()

    # ============================================================== scanning
    def current_options(self) -> ScanOptions:
        def split(v):
            return [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
        return ScanOptions(include_hidden=self.var_hidden.get(), respect_gitignore=self.var_git.get(),
                           use_default_ignores=self.var_defign.get(), ignore=split(self.var_ignore.get()),
                           extensions=split(self.var_ext.get()))

    def browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_path.get() or os.getcwd(), title="Choose a project folder")
        if d:
            self.var_path.set(os.path.normpath(d))
            self.start_scan()

    def start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        path = os.path.abspath(os.path.expanduser(self.var_path.get().strip().strip('"') or "."))
        if not os.path.isdir(path):
            messagebox.showerror("CodeTree", f"Folder not found:\n{path}")
            return
        self.var_path.set(path)
        opts = self.current_options()
        self.cancel_flag.clear()
        self.grow_token += 1
        self.scanning = True
        self.scan_count = 0
        self.btn_scan.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self.progress.start(12)
        self.var_status.set("Scanning…")
        self.select(None)

        def work():
            try:
                res = scan(path, opts,
                           progress=lambda n, p: self.queue.put(("progress", n, p)) if n % 25 == 0 else None,
                           cancel=self.cancel_flag.is_set)
                self.queue.put(("done", res))
            except Exception as e:
                self.queue.put(("error", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        self.cancel_flag.set()
        self.var_status.set("Stopping…")

    def poll(self) -> None:
        last = None
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg[0] == "progress":
                    last = msg
                else:
                    self.handle(msg)
        except queue.Empty:
            pass
        if last and self.scanning:
            self.scan_count = last[1]
            self.var_status.set(f"Scanning… {last[1]:,} files  ·  {os.path.basename(last[2])}")
        self.root.after(60, self.poll)

    def handle(self, msg) -> None:
        self.progress.stop()
        self.scanning = False
        self.btn_scan.state(["!disabled"])
        self.btn_cancel.state(["disabled"])
        self.clear_scan_ui()
        if msg[0] == "error":
            self.var_status.set("Scan failed")
            messagebox.showerror("CodeTree", msg[1])
            return
        old = self.result.root.path if self.result else None
        self.result = r = msg[1]
        if old != r.root.path:
            self.expanded = {r.root.path}
            self.show_all = set()
        self.hl_lang = None
        self.update_cards()
        self.draw_languages()
        self.draw_top()
        note = "Stopped early · " if r.cancelled else ""
        extra = f" · {r.skipped_binary} binary skipped" if r.skipped_binary else ""
        extra += f" · {len(r.errors)} unreadable" if r.errors else ""
        self.var_status.set(f"{note}{r.total_files:,} files, {r.total_lines:,} lines in {r.elapsed:.2f}s{extra}")
        recent = [r.root.path] + [p for p in self.cfg.get("recent", []) if p != r.root.path]
        self.cfg["recent"] = recent[:8]
        self.path_box.configure(values=self.cfg["recent"])
        self.grow()

    def update_cards(self) -> None:
        r = self.result
        vals = {"files": f"{r.total_files:,}", "folders": f"{r.folder_count:,}", "total": f"{r.total_lines:,}",
                "code": f"{r.root.code:,}", "comment": f"{r.root.comment:,}", "blank": f"{r.root.blank:,}",
                "size": format_size(r.root.size), "time": f"{r.elapsed:.2f}s"}
        for k, v in vals.items():
            self.cards[k].configure(text=v)

    # ===================================================== tree structure
    def _dir_files(self, d: DirNode):
        files = sorted(d.files, key=lambda f: (-f.total, f.name.lower()))
        if d.path in self.show_all:
            return files, 0
        return files[:FILE_CAP], max(0, len(files) - FILE_CAP)

    def _matches(self, d: DirNode, q: str, memo: dict) -> bool:
        r = memo.get(d.path)
        if r is None:
            r = q in d.name.lower() or any(q in f.name.lower() for f in d.files) or \
                any(self._matches(c, q, memo) for c in d.dirs)
            memo[d.path] = r
        return r

    def reconcile(self) -> None:
        """Rebuild the live tree from data + expanded set, reusing nodes so changes animate."""
        seen = set()
        if self.result:
            r = self.result.root
            q = self.query
            memo = {}
            total = r.total or 1
            maxfile = max((f.total for f in self.result.files), default=1) or 1

            budget = [SEARCH_CAP if q else 10 ** 9]

            def make(key, kind, parent, idx):
                budget[0] -= 1
                n = self.vnodes.get(key)
                if n is None:
                    n = VNode(key, kind)
                    self.vnodes[key] = n
                    if parent:
                        n.x, n.y = parent.x, parent.y
                    n.delay = min(idx * 0.03, 0.9)
                n.kind, n.dying, n.parent = kind, False, parent
                n.depth = parent.depth + 1 if parent else 0
                seen.add(key)
                return n

            def build(d: DirNode, parent, idx) -> VNode:
                n = make(d.path, "dir", parent, idx)
                n.data, n.name, n.total = d, d.name, d.total
                n.r = (20 if parent is None else 6 + 9 * math.sqrt(d.total / total))
                n.ew = 1.0 + 4.5 * (d.total / total) ** 0.35
                n.expanded = bool(q and self._matches(d, q, memo)) or d.path in self.expanded
                n.bcount = len(d.dirs) + len(d.files)
                kids = []
                if n.expanded:
                    for c in sorted(d.dirs, key=lambda c: -c.total):
                        if budget[0] <= 0:
                            break
                        if not q or self._matches(c, q, memo) or q in d.name.lower():
                            kids.append(build(c, n, len(kids)))
                    files, more = self._dir_files(d)
                    rank = 0
                    for f in files:
                        if budget[0] <= 0:
                            break
                        if q and q not in f.name.lower() and q not in d.name.lower():
                            continue
                        m = make(f.path, "file", n, len(kids))
                        m.data, m.name, m.total, m.lang = f, f.name, f.total, f.language
                        m.bcount, rank = rank, rank + 1
                        m.r = 3.2 + 6 * math.sqrt(f.total / maxfile)
                        m.ew = 0.9 + 1.6 * (f.total / total) ** 0.25
                        m.children, m.expanded = [], False
                        kids.append(m)
                    if more:
                        m = make(d.path + "\0more", "more", n, len(kids))
                        m.data, m.name, m.total, m.lang = d, f"+{more} more files", 0, None
                        m.r, m.ew = 4.5, 0.9
                        m.children, m.expanded = [], False
                        kids.append(m)
                n.children = kids
                return n

            self.root_node = build(r, None, 0)
        else:
            self.root_node = None
        for key, n in self.vnodes.items():
            if key not in seen:
                n.dying = True
                n.children = []
        if self.selected and self.selected.key not in seen:
            self.select(None)
        if self.hover and self.hover.key not in seen:
            self.set_hover(None)
        self.layout()
        self.mark_dirty()

    def layout(self) -> None:
        """Radial tree: trunk in the centre, folders branch outwards, files form leaf clusters."""
        root = self.root_node
        if not root:
            return
        unit, spacing, step = 34.0, 14.0, 175.0
        meta = {}

        def weigh(n: VNode) -> float:
            dirs = [c for c in n.children if c.kind == "dir"]
            leaves = [c for c in n.children if c.kind != "dir"]
            rc = spacing * math.sqrt(len(leaves)) + 12 if leaves else 0.0
            fw = 2 * rc / unit
            w = max(1.0, sum(weigh(c) for c in dirs) + fw)
            meta[n] = (dirs, leaves, rc, fw, w)
            return w

        total_w = weigh(root)
        r1 = max(210.0, unit * total_w / (2 * math.pi))

        def ring(d: int) -> float:
            return 0.0 if d == 0 else r1 + (d - 1) * step

        def place(n: VNode, a0: float, a1: float, off: float = 0.0) -> None:
            dirs, leaves, rc, fw, w = meta[n]
            mid = (a0 + a1) / 2
            rad = ring(n.depth) + off
            n.tx, n.ty = rad * math.cos(mid), rad * math.sin(mid)
            a, span = a0, a1 - a0
            crowded = len(dirs) > 22
            for i, c in enumerate(dirs):
                sub = span * meta[c][4] / w
                place(c, a, a + sub, 62.0 if crowded and i % 2 else 0.0)  # zig-zag so labels do not collide
                a += sub
            if leaves:  # files: a sunflower-pattern cluster of leaves at the end of the branch
                sub = span * fw / w
                am = a + sub / 2
                cr = max(ring(n.depth + 1), ring(n.depth) + rc + 34)
                cx, cy = cr * math.cos(am), cr * math.sin(am)
                for i, l in enumerate(leaves):
                    rr = spacing * math.sqrt(i + 0.5)
                    th = i * 2.39996323
                    l.tx, l.ty = cx + rr * math.cos(th), cy + rr * math.sin(th)

        place(root, -math.pi, math.pi)

    def visible_count_estimate(self, extra_dirs) -> int:
        live = sum(1 for n in self.vnodes.values() if not n.dying)
        add = 0
        for d in extra_dirs:
            files, more = self._dir_files(d)
            add += len(d.dirs) + len(files) + (1 if more else 0)
        return live + add

    def grow(self) -> None:
        """Animated growth: the tree sprouts level by level from the trunk."""
        self.grow_token += 1
        token = self.grow_token
        self.expanded = {self.result.root.path}
        self.reconcile()
        self.fit()
        level = [self.result.root]

        def step():
            nonlocal level
            if token != self.grow_token or not self.result:
                return
            nxt = [c for d in level for c in d.dirs if c.file_count]
            nxt.sort(key=lambda c: -c.total)
            if not nxt or self.visible_count_estimate(nxt) > GROW_CAP:
                return
            for d in nxt:
                self.expanded.add(d.path)
            level = nxt
            self.reconcile()
            self.fit()
            self.root.after(520, step)

        self.root.after(520, step)

    def expand_all(self) -> None:
        if not self.result:
            return
        self.grow_token += 1
        self.expanded = {self.result.root.path}
        frontier, count = [self.result.root], 0
        count = self.visible_count_estimate([self.result.root]) - len([n for n in self.vnodes.values() if not n.dying])
        while frontier and count < EXPAND_ALL_CAP:
            nxt = []
            for d in sorted((c for p in frontier for c in p.dirs), key=lambda c: -c.total):
                files, more = self._dir_files(d)
                cost = len(d.dirs) + len(files) + (1 if more else 0)
                if count + cost > EXPAND_ALL_CAP:
                    continue
                self.expanded.add(d.path)
                count += cost
                nxt.append(d)
            frontier = nxt
        self.reconcile()
        self.fit()

    def collapse_all(self) -> None:
        if not self.result:
            return
        self.grow_token += 1
        self.expanded = {self.result.root.path}
        self.reconcile()
        self.fit()

    def on_search(self) -> None:
        self.query = self.var_search.get().strip().lower()
        self.grow_token += 1
        self.reconcile()
        self.fit()

    # ================================================================ camera
    def mark_dirty(self) -> None:
        self.view_dirty = True

    def live_nodes(self):
        return [n for n in self.vnodes.values() if not n.dying]

    def fit(self) -> None:
        nodes = self.live_nodes()
        cw, ch = max(self.cv.winfo_width(), 200), max(self.cv.winfo_height(), 200)
        if not nodes:
            self.cam_t[:] = [1.0, cw / 2, ch / 2]
            return
        minx = min(n.tx for n in nodes) - 90
        maxx = max(n.tx for n in nodes) + 90
        miny = min(n.ty for n in nodes) - 70
        maxy = max(n.ty for n in nodes) + 60
        bw, bh = maxx - minx, maxy - miny
        s = max(0.08, min((cw - 60) / bw, (ch - 40) / bh, 1.25))
        self.cam_t[:] = [s, (cw - bw * s) / 2 - minx * s, (ch - bh * s) / 2 - miny * s]
        self.mark_dirty()

    def ensure_visible(self, n: VNode) -> None:
        cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
        sub, stack = [], [n]
        while stack:
            m = stack.pop()
            sub.append(m)
            stack.extend(m.children)
        s, ox, oy = self.cam_t
        x0 = min(m.tx for m in sub) * s + ox - 60
        x1 = max(m.tx for m in sub) * s + ox + 60
        y0 = min(m.ty for m in sub) * s + oy - 60
        y1 = max(m.ty for m in sub) * s + oy + 40
        dx = dy = 0.0
        if x1 > cw:
            dx = cw - x1
        if x0 + dx < 0:
            dx = -x0
        if y1 > ch:
            dy = ch - y1
        if y0 + dy < 0:
            dy = -y0
        self.cam_t[1] += dx
        self.cam_t[2] += dy

    def center_on(self, n: VNode) -> None:
        cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
        s = max(self.cam_t[0], 0.7)
        self.cam_t[:] = [s, cw * 0.4 - n.tx * s, ch / 2 - n.ty * s]

    def zoom_at(self, x, y, f) -> None:
        s, ox, oy = self.cam_t
        ns = max(0.08, min(4.0, s * f))
        f = ns / s
        self.cam_t[:] = [ns, x - (x - ox) * f, y - (y - oy) * f]
        self.mark_dirty()

    # ============================================================== events
    def hit(self, x, y):
        s, ox, oy = self.cam
        best, bd = None, 1e9
        for n in self.vnodes.values():
            if n.dying or n.g < 0.4:
                continue
            rs = max(7.0, n.r * max(0.5, s ** 0.7)) + 4
            d = (n.x * s + ox - x) ** 2 + (n.y * s + oy - y) ** 2
            if d < rs * rs and d < bd:
                best, bd = n, d
        return best

    def on_press(self, e) -> None:
        self.press = (e.x, e.y, self.hit(e.x, e.y), False)

    def on_drag(self, e) -> None:
        if not self.press:
            return
        x0, y0, n, moved = self.press
        if not moved and abs(e.x - x0) + abs(e.y - y0) < 5:
            return
        dx, dy = e.x - x0, e.y - y0
        for c in (self.cam, self.cam_t):
            c[1] += dx
            c[2] += dy
        self.press = (e.x, e.y, n, True)
        self.cv.configure(cursor="fleur")
        self.mark_dirty()

    def on_release(self, e) -> None:
        self.cv.configure(cursor="arrow")
        if not self.press:
            return
        _, _, n, moved = self.press
        self.press = None
        if moved:
            return
        if n is None:
            self.select(None)
            return
        now = time.perf_counter()
        last_n, last_t = self.last_click
        self.last_click = (n, now)
        if last_n is n and now - last_t < 0.32:
            if n.kind == "file":
                _open_path(n.data.path)
            return
        self.select(n)
        if n.kind == "dir":
            if n.key in self.expanded:
                if n.parent is not None:  # the trunk always stays open
                    self.expanded.discard(n.key)
            else:
                self.expanded.add(n.key)
            self.grow_token += 1
            self.reconcile()
            self.ensure_visible(n)
        elif n.kind == "more":
            self.show_all.add(n.data.path)
            self.reconcile()
            self.ensure_visible(n.parent)

    def on_motion(self, e) -> None:
        if self.press and self.press[3]:
            return
        self.set_hover(self.hit(e.x, e.y))

    def set_hover(self, n) -> None:
        if n is self.hover:
            return
        self.hover = n
        self.hot = set()
        m = n
        while m is not None:
            self.hot.add(m)
            m = m.parent
        self.cv.configure(cursor="hand2" if n else "arrow")
        self.update_card()
        self.mark_dirty()

    def select(self, n) -> None:
        self.selected = n
        self.update_card()
        self.mark_dirty()

    def on_context(self, e) -> None:
        n = self.hit(e.x, e.y)
        if n and n.kind in ("dir", "file"):
            self.select(n)
            self.ctx.tk_popup(e.x_root, e.y_root)

    def ctx_act(self, what) -> None:
        n = self.selected
        if not n or n.kind not in ("dir", "file"):
            return
        path = n.data.path
        if what == "copy":
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self.var_status.set("Path copied to clipboard")
        else:
            _open_path(path, reveal=(what == "reveal"))

    def update_card(self) -> None:
        n = self.hover or self.selected
        if not n or not self.result:
            self.card.place_forget()
            return
        total = self.result.total_lines or 1
        base = self.result.root.path
        if n.kind == "more":
            self.card_title.configure(text=n.name)
            self.card_sub.configure(text="click to show every file in this folder")
            self.card_stats.configure(text="")
        else:
            d = n.data
            rel = os.path.relpath(d.path, base).replace(os.sep, "/")
            icon = "📁" if n.kind == "dir" else "📄"
            self.card_title.configure(text=f"{icon} {n.name}")
            self.card_sub.configure(text=("/" if rel == "." else rel) + (f"   ·   {d.language}" if n.kind == "file" else
                                                                            f"   ·   {d.file_count:,} files"))
            self.card_stats.configure(
                text=f"{d.total:,} lines  ({d.total / total:.1%} of project)\n"
                     f"code {d.code:,}  ·  comments {d.comment:,}  ·  blank {d.blank:,}\n"
                     f"{format_size(d.size)}")
        self.card.place(relx=0.0, rely=1.0, x=14, y=-14, anchor="sw")

    # ============================================================ animation
    def tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.06, now - self.last_t)
        self.last_t = now
        moving = self.step(dt)
        if moving or self.view_dirty:
            self.render()
            self.view_dirty = False
        self.ambient(now)
        self.root.after(15 if moving else 33, self.tick)

    def step(self, dt: float) -> bool:
        moving = False
        k = 1 - math.exp(-dt * 7.5)
        kg = 1 - math.exp(-dt * 9)
        for i in range(3):
            d = self.cam_t[i] - self.cam[i]
            if abs(d) > (0.0005 if i == 0 else 0.3):
                self.cam[i] += d * (1 - math.exp(-dt * 8))
                moving = True
            else:
                self.cam[i] = self.cam_t[i]
        dead = []
        for n in sorted(self.vnodes.values(), key=lambda n: n.depth):
            p = n.parent
            if n.dying:
                tx, ty, gt = (p.x, p.y, 0.0) if p else (n.x, n.y, 0.0)
            elif n.delay > 0:
                n.delay -= dt
                if p:
                    n.x, n.y = p.x, p.y
                moving = True
                continue
            else:
                tx, ty, gt = n.tx, n.ty, 1.0
            dx, dy, dg = tx - n.x, ty - n.y, gt - n.g
            if abs(dx) > 0.05 or abs(dy) > 0.05 or abs(dg) > 0.01:
                n.x += dx * k
                n.y += dy * k
                n.g += dg * kg
                moving = True
            else:
                n.x, n.y, n.g = tx, ty, gt
            if n.dying and n.g < 0.04:
                dead.append(n)
        for n in dead:
            for it in (n.e, n.gl, n.o, n.t, n.b):
                if it:
                    self.cv.delete(it)
            del self.vnodes[n.key]
            moving = True
        if self.scanning:
            moving = True
        return moving

    def font(self, size: int, bold=False):
        f = self.fonts.get((size, bold))
        if f is None:
            f = self.fonts[(size, bold)] = tkfont.Font(family=self.family, size=size, weight="bold" if bold else "normal")
        return f

    def make_items(self, n: VNode) -> None:
        cv = self.cv
        if n.parent is not None:
            n.e = cv.create_line(0, 0, 0, 0, 1, 1, capstyle="round", joinstyle="round", tags="edge")
        n.gl = cv.create_oval(0, 0, 0, 0, width=1, tags="glow")
        n.o = cv.create_oval(0, 0, 0, 0, width=0, tags="node")
        n.t = cv.create_text(0, 0, text=n.name, anchor="w",
                             font=self.font(self.cur_fs, n.kind == "dir"), tags="text")
        n.b = cv.create_text(0, 0, text="", font=self.font(max(7, self.cur_fs - 2), True), tags="text")
        n.cstate = None

    def node_colors(self, n: VNode):
        t = self.t
        bg = t["bg"]
        pal = t["branch"]
        depth_col = pal[min(n.depth, len(pal) - 1)]
        base = depth_col if n.kind == "dir" else (language_color(n.lang) if n.kind == "file" else t["muted"])
        hot = n in self.hot
        sel = n is self.selected
        q, hl = self.query, self.hl_lang
        match = bool(q) and q in n.name.lower()
        dim = False
        if hl and n.kind == "file":
            match = n.lang == hl
            dim = not match
        elif q and not match and n.kind != "dir":
            dim = True
        if dim and not hot:
            base = mix(base, bg, 0.25)
        pp = pal[min(max(n.depth - 1, 0), len(pal) - 1)]
        edge = mix(t["fg"], bg, 0.85) if hot else (mix(base, bg, 0.32) if n.kind == "file" else mix(pp, bg, 0.75))
        ring = t["fg"] if (sel or hot) else (t["glow"] if match else mix(base, bg, 0.35))
        lab = t["fg"] if not dim else mix(t["fg"], bg, 0.3)
        return base, edge, ring, lab

    @staticmethod
    def radial(x, y, fx, fy):
        """Unit vector pointing away from the trunk at (x, y); falls back to the direction of (fx, fy)."""
        d = math.hypot(x, y)
        if d > 1:
            return x / d, y / d
        d = math.hypot(fx - x, fy - y) or 1.0
        return (fx - x) / d, (fy - y) / d

    def render(self) -> None:
        cv = self.cv
        s, ox, oy = self.cam
        cw, ch = cv.winfo_width(), cv.winfo_height()
        fs = max(8, min(18, round(10 * s)))
        if fs != self.cur_fs:
            self.cur_fs = fs
            for n in self.vnodes.values():
                if n.t:
                    cv.itemconfigure(n.t, font=self.font(fs, n.kind == "dir"))
                    cv.itemconfigure(n.b, font=self.font(max(7, fs - 2), True))
        created = False
        zs = max(0.5, s ** 0.7)
        ws = max(0.5, s ** 0.8)
        bg = self.t["bg"]
        for n in self.vnodes.values():
            if n.o is None:
                self.make_items(n)
                created = True
            px, py = n.x * s + ox, n.y * s + oy
            p = n.parent
            qx, qy = (p.x * s + ox, p.y * s + oy) if p else (px, py)
            vis = (-100 < px < cw + 280 and -60 < py < ch + 60) or (-100 < qx < cw + 280 and -60 < qy < ch + 60)
            if vis != n.shown:
                n.shown = vis
                st = "normal" if vis else "hidden"
                for it in (n.e, n.gl, n.o, n.t, n.b):
                    if it:
                        cv.itemconfigure(it, state=st)
                n.lab_vis = None
            if not vis:
                continue
            g = max(n.g, 0.001)
            rs = n.r * g * zs
            cv.coords(n.o, px - rs, py - rs, px + rs, py + rs)
            gr = rs + 3 + 1.5 * zs
            cv.coords(n.gl, px - gr, py - gr, px + gr, py + gr)
            if n.e:  # branch: bezier leaving the parent and arriving at the child along the radial direction
                u0x, u0y = self.radial(p.x, p.y, n.x, n.y)
                u3x, u3y = self.radial(n.x, n.y, p.x, p.y)
                length = math.hypot(px - qx, py - qy)
                k = length * 0.42
                c1x, c1y, c2x, c2y = qx + u0x * k, qy + u0y * k, px - u3x * k, py - u3y * k
                steps = max(3, min(10, int(length / 22)))
                pts = []
                for i in range(steps + 1):
                    tt = i / steps
                    u = 1 - tt
                    a, b, c, d = u * u * u, 3 * u * u * tt, 3 * u * tt * tt, tt * tt * tt
                    pts.append(a * qx + b * c1x + c * c2x + d * px)
                    pts.append(a * qy + b * c1y + c * c2y + d * py)
                cv.coords(n.e, *pts)
                w = round(max(0.8, n.ew * ws * g), 1)
                if w != n.ewc:
                    n.ewc = w
                    cv.itemconfigure(n.e, width=w)
            if n.kind == "dir":
                if p is None:
                    cv.coords(n.t, px, py - rs - 14)
                    anc = "s"
                else:
                    ux, uy = self.radial(n.x, n.y, 0, 0)
                    cv.coords(n.t, px + ux * (rs + 8), py + uy * (rs + 8))
                    anc = "w" if ux > 0.4 else "e" if ux < -0.4 else ("n" if uy > 0 else "s")
                if anc != n.anc:
                    n.anc = anc
                    cv.itemconfigure(n.t, anchor=anc)
            else:
                cv.coords(n.t, px + rs + 7, py)
            cv.coords(n.b, px, py)
            hot_or_sel = n in self.hot or n is self.selected
            if n.kind == "dir":
                lv = n.g > 0.6 and (s >= 0.25 or n.depth <= 1 or hot_or_sel)
            elif n.kind == "file":  # only the biggest leaves are named; hover reveals the rest
                lv = n.g > 0.6 and (hot_or_sel or (s >= 0.8 and n.bcount < 2) or (s >= 2.2 and n.bcount < 30))
            else:
                lv = n.g > 0.6 and s >= 0.5
            bv = n.kind == "dir" and not n.expanded and n.bcount and rs > 9 and n.g > 0.6
            state = (lv, bv)
            if n.lab_vis != state:
                n.lab_vis = state
                cv.itemconfigure(n.t, state="normal" if lv else "hidden")
                cv.itemconfigure(n.b, state="normal" if bv else "hidden", text=str(n.bcount) if bv else "")
            cs = (self.theme_name, n in self.hot, n is self.selected, self.query, self.hl_lang, n.expanded)
            if n.cstate != cs:
                n.cstate = cs
                base, edge, ring, lab = self.node_colors(n)
                cv.itemconfigure(n.o, fill=base)
                cv.itemconfigure(n.gl, outline=ring, width=2 if (n in self.hot or n is self.selected) else 1)
                cv.itemconfigure(n.t, fill=lab)
                cv.itemconfigure(n.b, fill=bg)
                if n.e:
                    cv.itemconfigure(n.e, fill=edge)
        if created:
            for tag in ("edge", "glow", "node", "text"):
                cv.tag_raise(tag)
            cv.tag_lower("firefly")
        self.draw_scan_ui(cw, ch)
        if not self.vnodes and not self.scanning:
            self.draw_empty(cw, ch)
        else:
            cv.delete("empty")

    def draw_empty(self, cw, ch) -> None:
        if self.cv.find_withtag("empty"):
            self.cv.coords("empty", cw / 2, ch / 2)
            return
        self.cv.create_text(cw / 2, ch / 2, tags="empty", fill=self.t["muted"], font=self.f_bold,
                            text="🌱  Pick a project folder and press Scan to grow its tree")

    def clear_scan_ui(self) -> None:
        self.cv.delete("scanui")
        self.scan_items = []

    def draw_scan_ui(self, cw, ch) -> None:
        if not self.scanning:
            return
        cv, t = self.cv, self.t
        if not self.scan_items:
            self.scan_items = [cv.create_oval(0, 0, 0, 0, outline=t["accent"], width=2, tags="scanui"),
                               cv.create_oval(0, 0, 0, 0, outline=t["accent"], width=1, tags="scanui"),
                               cv.create_text(0, 0, fill=t["fg"], font=self.f_bold, tags="scanui")]
        ph = time.perf_counter() * 1.6
        cx, cy = cw / 2, ch / 2
        for i, it in enumerate(self.scan_items[:2]):
            r = 22 + 26 * ((ph + i * 0.5) % 1.0)
            cv.coords(it, cx - r, cy - r, cx + r, cy + r)
            cv.itemconfigure(it, outline=mix(t["accent"], t["bg"], 1 - ((ph + i * 0.5) % 1.0)))
        cv.coords(self.scan_items[2], cx, cy + 74)
        cv.itemconfigure(self.scan_items[2], text=f"Reading your code…  {self.scan_count:,} files")
        cv.tag_raise("scanui")

    def ambient(self, now: float) -> None:
        """Drifting fireflies behind the tree."""
        cv = self.cv
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 50:
            return
        if not self.fireflies:
            for _ in range(34):
                self.fireflies.append(dict(x=random.random(), y=random.random(), vx=random.uniform(-.012, .012),
                                           vy=random.uniform(-.02, -.004), ph=random.uniform(0, 6.3),
                                           sp=random.uniform(.6, 1.8), r=random.choice((1.3, 1.8, 2.4)),
                                           id=cv.create_oval(0, 0, 0, 0, width=0, tags="firefly")))
            cv.tag_lower("firefly")
        dt = 0.033
        for f in self.fireflies:
            f["x"] = (f["x"] + f["vx"] * dt + math.sin(now * .4 + f["ph"]) * .0004) % 1.0
            f["y"] = (f["y"] + f["vy"] * dt) % 1.0
            x, y, r = f["x"] * cw, f["y"] * ch, f["r"]
            cv.coords(f["id"], x - r, y - r, x + r, y + r)
            a = 0.12 + 0.38 * (0.5 + 0.5 * math.sin(now * f["sp"] + f["ph"]))
            cv.itemconfigure(f["id"], fill=mix(self.t["glow"], self.t["bg"], a))

    # ============================================================ side panel
    def _empty(self, cv, text) -> None:
        cv.create_text(cv.winfo_width() / 2, 80, text=text, fill=self.t["muted"], font=self.f_bold, width=240)

    def draw_languages(self) -> None:
        cv, t = self.cv_lang, self.t
        cv.delete("all")
        self.lang_rows = []
        w = max(cv.winfo_width(), 280)
        if not self.result or not self.result.languages:
            return self._empty(cv, "Scan a project to see the language breakdown")
        langs = sorted_languages(self.result)
        total = sum(l.total for l in langs) or 1
        pad = 14
        cv.create_text(pad, 22, text="Composition by language", anchor="w", fill=t["fg"], font=self.f_bold)
        x, bw = pad, w - 2 * pad
        for l in langs:
            seg = bw * l.total / total
            if seg >= 1:
                cv.create_rectangle(x, 40, x + seg, 60, fill=language_color(l.name), width=0)
            x += seg
        cv.create_text(pad, 74, text="click a language to light up its leaves", anchor="w", fill=t["muted"],
                       font=self.f_small)
        y, top = 96, max(l.total for l in langs)
        for l in langs:
            color = language_color(l.name)
            if self.hl_lang == l.name:
                cv.create_rectangle(4, y - 4, w - 4, y + 40, fill=t["select"], width=0)
            cv.create_oval(pad, y + 4, pad + 10, y + 14, fill=color, width=0)
            cv.create_text(pad + 18, y + 9, text=l.name, anchor="w", fill=t["fg"], font=self.f_bold)
            cv.create_text(w - pad, y + 9, anchor="e", fill=t["muted"], font=self.f_small,
                           text=f"{l.total:,} · {l.total / total:.1%}")
            cv.create_rectangle(pad, y + 24, w - pad, y + 30, fill=t["card"], width=0)
            cv.create_rectangle(pad, y + 24, pad + max(2, (w - 2 * pad) * l.total / top), y + 30, fill=color, width=0)
            self.lang_rows.append((y - 4, y + 40, l.name))
            y += 48
        y += 8
        r = self.result.root
        cv.create_text(pad, y, text="Line types", anchor="w", fill=t["fg"], font=self.f_bold)
        x = pad
        parts = (("Code", r.code, "code"), ("Comments", r.comment, "comment"), ("Blank", r.blank, "blank"))
        for _, n, key in parts:
            seg = bw * n / (r.total or 1)
            if seg >= 1:
                cv.create_rectangle(x, y + 16, x + seg, y + 32, fill=t[key], width=0)
            x += seg
        ly = y + 46
        for label, n, key in parts:
            cv.create_rectangle(pad, ly, pad + 10, ly + 10, fill=t[key], width=0)
            cv.create_text(pad + 16, ly + 5, anchor="w", fill=t["muted"], font=self.f_small,
                           text=f"{label}  {n / (r.total or 1):.1%}")
            ly += 18
        cv.configure(scrollregion=(0, 0, w, ly + 10))

    def on_lang_click(self, e) -> None:
        y = self.cv_lang.canvasy(e.y)
        for y0, y1, name in self.lang_rows:
            if y0 <= y <= y1:
                self.hl_lang = None if self.hl_lang == name else name
                self.draw_languages()
                self.mark_dirty()
                return

    def draw_top(self) -> None:
        cv, t = self.cv_top, self.t
        cv.delete("all")
        self.top_rows = []
        w = max(cv.winfo_width(), 280)
        if not self.result or not self.result.files:
            return self._empty(cv, "No files yet")
        files = sorted(self.result.files, key=lambda f: f.total, reverse=True)[:15]
        top = files[0].total or 1
        pad, y = 14, 22
        cv.create_text(pad, y, text="Top 15 largest files", anchor="w", fill=t["fg"], font=self.f_bold)
        y += 22
        cv.create_text(pad, y, text="click one to find it in the tree", anchor="w", fill=t["muted"], font=self.f_small)
        y += 22
        base = self.result.root.path
        for f in files:
            rel = os.path.relpath(f.path, base).replace(os.sep, "/")
            if len(rel) > 34:
                rel = "…" + rel[-33:]
            cv.create_text(pad, y + 8, text=rel, anchor="w", fill=t["fg"], font=self.f_small)
            cv.create_text(w - pad, y + 8, text=f"{f.total:,}", anchor="e", fill=t["muted"], font=self.f_small)
            cv.create_rectangle(pad, y + 20, w - pad, y + 25, fill=t["card"], width=0)
            cv.create_rectangle(pad, y + 20, pad + max(2, (w - 2 * pad) * f.total / top), y + 25,
                                fill=language_color(f.language), width=0)
            self.top_rows.append((y - 4, y + 32, f))
            y += 38
        cv.configure(scrollregion=(0, 0, w, y))

    def on_top_click(self, e) -> None:
        y = self.cv_top.canvasy(e.y)
        for y0, y1, f in self.top_rows:
            if y0 <= y <= y1:
                self.reveal(f.path)
                return

    def reveal(self, path: str) -> None:
        """Expand every ancestor of ``path`` and fly the camera to it."""
        if not self.result:
            return
        self.grow_token += 1
        self.var_search.set("")
        self.query = ""
        cur = os.path.dirname(path)
        base = self.result.root.path
        while True:
            self.expanded.add(cur)
            if os.path.normpath(cur) == os.path.normpath(base) or os.path.dirname(cur) == cur:
                break
            cur = os.path.dirname(cur)
        parent = os.path.dirname(path)
        files = sorted(self.result.files, key=lambda f: (-f.total, f.name.lower()))
        in_dir = [f.path for f in files if os.path.dirname(f.path) == parent]
        if path in in_dir and in_dir.index(path) >= FILE_CAP:
            self.show_all.add(parent)
        self.reconcile()
        n = self.vnodes.get(path)
        if n and not n.dying:
            self.select(n)
            self.center_on(n)

    # =============================================================== export
    def export(self, kind: str) -> None:
        if not self.result:
            messagebox.showinfo("CodeTree", "Run a scan first.")
            return
        fn = {"json": to_json, "csv": to_csv, "md": to_markdown}[kind]
        ext = {"json": ".json", "csv": ".csv", "md": ".md"}[kind]
        path = filedialog.asksaveasfilename(defaultextension=ext, initialfile=f"codetree-report{ext}",
                                            filetypes=[(kind.upper(), f"*{ext}"), ("All files", "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(fn(self.result))
                self.var_status.set(f"Saved {path}")
            except OSError as e:
                messagebox.showerror("CodeTree", str(e))

    def on_close(self) -> None:
        self.cancel_flag.set()
        self.grow_token += 1
        self.cfg["theme"] = self.theme_name
        _save_config(self.cfg)
        self.root.destroy()


def run_gui(path: str = ".", options: ScanOptions = None) -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, path, options or ScanOptions())
    root.mainloop()
