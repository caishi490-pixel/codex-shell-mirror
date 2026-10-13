# -*- coding: utf-8 -*-
"""Codex Shell Mirror · 终端镜像（只读监督版）。

把 Codex 每次 shell 执行的「命令 + 输出 + 退出码 + 耗时」实时投到桌面小窗，
适合常驻副屏，随时监督 Codex 对你的电脑做了什么。

只读监听本地会话日志（默认 ~/.codex/sessions 下的 rollout-*.jsonl）：
不执行命令、不改任何状态、不联网。

操作：
    拖动窗口   按住窗口顶部拖动（位置自动记忆）
    缩放窗口   拖窗口边缘 / 四角（大小自动记忆）
    字号缩放   Ctrl + 滚轮，或 Ctrl +/-；Ctrl 0 复位
    右键       菜单（清空 / 暂停监听 / 字号 / 退出）
    Esc        退出

自启方法与全部参数见 README.md。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import socket
import sys
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta

VERSION = "1.0.0"
APP_NAME = "Codex Shell Mirror"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "codex_shell_mirror.log"
STATE_FILE = SCRIPT_DIR / "codex_shell_mirror_state.json"

EXEC_NAMES = {"exec_command", "exec", "shell", "run_command"}
RECENT_SECONDS = 180
STALE_SECONDS = 900
POLL_MS = 400
MAX_KEEP_LINES = 1500
BODY_MAX_LINES = 80
MIN_W, MIN_H = 420, 260
EDGE_ZONE = 6
ZOOM_MIN, ZOOM_MAX, ZOOM_DEFAULT = 6, 22, 9

# ---------- 配色 ----------
BORDER = "#2b333d"
BG = "#0b0e13"
HDR = "#141a22"
BAR = "#10151c"
CHIP_BG = "#1b2430"
FG = "#cdd6e0"
CMD_FG = "#7ee787"
ACCENT = "#39d0d8"
DIM = "#7d8590"
FAINT = "#39424d"

if os.name == "nt":
    MONO_FAMILY = "Consolas"
    UI_FAMILY = "Microsoft YaHei UI"
elif sys.platform == "darwin":
    MONO_FAMILY = "Menlo"
    UI_FAMILY = "PingFang SC"
else:
    MONO_FAMILY = "DejaVu Sans Mono"
    UI_FAMILY = "Noto Sans CJK SC"

UI_FONT_B = (UI_FAMILY, 9, "bold")
UI_FONT_SMALL = (UI_FAMILY, 8)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
WALL_RE = re.compile(r"Wall time: ([\d.]+) seconds")
EXIT_RE = re.compile(r"Process exited with code (-?\d+)")


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def default_sessions_dir() -> pathlib.Path:
    env = os.environ.get("CODEX_HOME")
    root = pathlib.Path(env).expanduser() if env else pathlib.Path.home() / ".codex"
    return root / "sessions"


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="codex_shell_mirror",
        description="把 Codex 每次 shell 执行（命令 / 输出 / 退出码 / 耗时）实时镜像到桌面小窗，只读。")
    ap.add_argument("--sessions-dir", default=None,
                    help="Codex 会话日志目录（默认 ~/.codex/sessions，也认 CODEX_HOME 环境变量）")
    ap.add_argument("--lock-port", type=int, default=47811,
                    help="单实例锁端口（默认 47811）")
    ap.add_argument("--title", default="Codex · 终端镜像", help="窗口标题")
    ap.add_argument("--width", type=int, default=800, help="首次启动宽度（默认 800，之后自动记忆）")
    ap.add_argument("--height", type=int, default=466, help="首次启动高度（默认 466，之后自动记忆）")
    ap.add_argument("--font-size", type=int, default=ZOOM_DEFAULT, help="首次启动字号（默认 9，之后自动记忆）")
    ap.add_argument("--version", action="version", version="%s v%s" % (APP_NAME, VERSION))
    return ap.parse_args(argv)


class Mirror:
    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.sessions_root = (pathlib.Path(cfg.sessions_dir).expanduser()
                              if cfg.sessions_dir else default_sessions_dir())
        self._lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._lock.bind(("127.0.0.1", cfg.lock_port))
            self._lock.listen(1)
        except OSError:
            log("another instance already running; exit")
            raise SystemExit(0)

        self.offsets: dict[str, int] = {}
        self.buffers: dict[str, str] = {}
        self.pending: dict[str, dict] = {}
        self.count = 0
        self.fail_count = 0
        self.paused = False
        self.last_activity = ""
        self.session_tag = ""
        self._pulse = 0
        self._dot_colors = ("#2ea043", "#1d5c2e")

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.95)
        except Exception:
            pass
        self._hover_edge = None
        self._resize_mode = None
        self._rs = None
        self._drag_active = False
        self._cursor_now = ""
        w, h, px, py, self._zoom = self._load_geo()
        self.root.geometry("%dx%d+%d+%d" % (w, h, px, py))
        self.root.configure(bg=BORDER)
        self.root.bind("<Motion>", self._on_motion)
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_b1)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        for seq, fn in (("<Control-plus>", lambda e: self._zoom_by(1)),
                        ("<Control-equal>", lambda e: self._zoom_by(1)),
                        ("<Control-minus>", lambda e: self._zoom_by(-1)),
                        ("<Control-0>", lambda e: self._zoom_set(ZOOM_DEFAULT)),
                        ("<Control-KP_Add>", lambda e: self._zoom_by(1)),
                        ("<Control-KP_Subtract>", lambda e: self._zoom_by(-1))):
            try:
                self.root.bind(seq, fn)
            except Exception:
                pass

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=1, pady=1)

        # ---------- 顶栏 ----------
        header = tk.Frame(main, bg=HDR, height=30)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.dot = tk.Canvas(header, width=12, height=12, bg=HDR, highlightthickness=0)
        self._dot_item = self.dot.create_oval(2, 2, 10, 10, fill="#2ea043", outline="")
        self.dot.pack(side="left", padx=(10, 4))
        self.lbl_title = tk.Label(header, text=cfg.title, fg="#e6edf3", bg=HDR, font=UI_FONT_B)
        self.lbl_title.pack(side="left")
        self.lbl_chip = tk.Label(header, text="监听中 · 空闲", fg=ACCENT, bg=CHIP_BG, font=UI_FONT_SMALL, padx=8, pady=1)
        self.lbl_chip.pack(side="left", padx=(10, 0))
        self.lbl_clock = tk.Label(header, text="--:--:--", fg=DIM, bg=HDR, font=(MONO_FAMILY, 9))
        self.lbl_clock.pack(side="right", padx=10)

        # ---------- 文本区 ----------
        self.text = tk.Text(main, bg=BG, fg=FG, font=(MONO_FAMILY, self._zoom), bd=0,
                            highlightthickness=0, wrap="none", padx=10, pady=8,
                            selectbackground="#264f78")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("cmd", foreground=CMD_FG)
        self.text.tag_configure("meta", foreground=DIM)
        self.text.tag_configure("faint", foreground=FAINT)
        self.text.tag_configure("title2", foreground=ACCENT, font=(UI_FAMILY, self._zoom, "bold"))
        self.text.tag_configure("runc", foreground="#58a6ff")
        self.text.tag_configure("okc", foreground="#3fb950")
        self.text.tag_configure("badc", foreground="#f47067")
        self.text.tag_configure("badge_ok", foreground="#56d364", background="#0f2e1b")
        self.text.tag_configure("badge_bad", foreground="#ff7b72", background="#3a1519")
        self.text.configure(state="disabled")

        # ---------- 底栏 ----------
        bar = tk.Frame(main, bg=BAR, height=24)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.lbl_session = tk.Label(bar, text="只读镜像 · 会话 #------", fg=DIM, bg=BAR, font=UI_FONT_SMALL)
        self.lbl_session.pack(side="left", padx=10)
        self.lbl_count = tk.Label(bar, text="已捕获 0 · 失败 0 · 最近 --:--:--", fg=DIM, bg=BAR, font=UI_FONT_SMALL)
        self.lbl_count.pack(side="right", padx=10)

        drag_widgets = (header, self.dot, self.lbl_title, self.lbl_chip, self.lbl_clock,
                        bar, self.lbl_session, self.lbl_count)
        for wid in drag_widgets:
            wid.bind("<ButtonPress-1>", self._drag_start)
            wid.bind("<B1-Motion>", self._drag_move)
            wid.bind("<ButtonRelease-1>", self._save_pos)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="清空", command=self.clear)
        self.menu.add_command(label="暂停 / 继续监听", command=self.toggle_pause)
        self.menu.add_separator()
        self.menu.add_command(label="放大字号  (Ctrl +)", command=lambda: self._zoom_by(1))
        self.menu.add_command(label="缩小字号  (Ctrl -)", command=lambda: self._zoom_by(-1))
        self.menu.add_command(label="字号复位  (Ctrl 0)", command=lambda: self._zoom_set(ZOOM_DEFAULT))
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)
        for wid in (self.root, self.text, header, self.dot, self.lbl_title, self.lbl_chip,
                    self.lbl_clock, bar, self.lbl_session, self.lbl_count):
            wid.bind("<Button-3>", self._popup)
        self.root.bind("<Escape>", lambda e: self.quit())
        self._dxy = (0, 0)

        self._show_empty()
        self._refresh_state()
        log("started v%s pid=%d watching=%s" % (VERSION, os.getpid(), self.sessions_root))

    # ---------- 状态 ----------
    def _refresh_state(self):
        running = len(self.pending)
        if self.paused:
            self.lbl_chip.configure(text="已暂停", fg=DIM)
            self._dot_colors = ("#6e7681", "#6e7681")
        elif running:
            self.lbl_chip.configure(text="执行中 · %d" % running, fg=ACCENT)
            self._dot_colors = ("#39d0d8", "#1e6f77")
        else:
            self.lbl_chip.configure(text="监听中 · 空闲", fg=ACCENT)
            self._dot_colors = ("#2ea043", "#1d5c2e")
        self.lbl_count.configure(text="已捕获 %d · 失败 %d · 最近 %s"
                                      % (self.count, self.fail_count, self.last_activity or "--:--:--"))

    def _tick(self):
        self._pulse ^= 1
        try:
            self.dot.itemconfigure(self._dot_item, fill=self._dot_colors[self._pulse])
        except Exception:
            pass
        self.lbl_clock.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.root.after(700, self._tick)

    # ---------- 窗口 ----------
    def _drag_start(self, ev):
        if self._hover_edge:
            self._drag_active = False
            return
        self._drag_active = True
        self._dxy = (ev.x, ev.y)

    def _drag_move(self, ev):
        if not self._drag_active:
            return
        x = self.root.winfo_x() + ev.x - self._dxy[0]
        y = self.root.winfo_y() + ev.y - self._dxy[1]
        self.root.geometry("+%d+%d" % (x, y))

    # ---------- 缩放 ----------
    _CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",
                "nw": "size_nw_se", "se": "size_nw_se",
                "ne": "size_ne_sw", "sw": "size_ne_sw"}

    def _edge_at(self, xr, yr):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if not (x - EDGE_ZONE <= xr <= x + w + EDGE_ZONE
                and y - EDGE_ZONE <= yr <= y + h + EDGE_ZONE):
            return None
        left = xr <= x + EDGE_ZONE
        right = xr >= x + w - EDGE_ZONE
        top = yr <= y + EDGE_ZONE
        bottom = yr >= y + h - EDGE_ZONE
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return None

    def _set_cursor(self, edge):
        name = self._CURSORS.get(edge, "") if edge else ""
        if name == self._cursor_now:
            return
        self._cursor_now = name
        try:
            self.root.configure(cursor=name)
        except Exception:
            pass
        try:
            self.text.configure(cursor=name or "xterm")
        except Exception:
            pass

    def _on_motion(self, ev):
        if self._resize_mode or self._drag_active:
            return
        self._hover_edge = self._edge_at(ev.x_root, ev.y_root)
        self._set_cursor(self._hover_edge)

    def _on_press(self, ev):
        edge = self._edge_at(ev.x_root, ev.y_root)
        if edge:
            self._resize_mode = edge
            self._rs = {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                        "w": self.root.winfo_width(), "h": self.root.winfo_height(),
                        "px": ev.x_root, "py": ev.y_root}

    def _on_b1(self, ev):
        if not self._resize_mode or not self._rs:
            return
        r = self._rs
        m = self._resize_mode
        dx = ev.x_root - r["px"]
        dy = ev.y_root - r["py"]
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        if "e" in m:
            w = max(MIN_W, r["w"] + dx)
        if "w" in m:
            w = max(MIN_W, r["w"] - dx)
            x = r["x"] + (r["w"] - w)
        if "s" in m:
            h = max(MIN_H, r["h"] + dy)
        if "n" in m:
            h = max(MIN_H, r["h"] - dy)
            y = r["y"] + (r["h"] - h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _on_release(self, ev):
        self._drag_active = False
        if self._resize_mode:
            self._resize_mode = None
            self._rs = None
            self._save_pos()

    def _load_geo(self):
        w, h, x, y, zoom = (self.cfg.width, self.cfg.height, 40, 120, self.cfg.font_size)
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            x = int(d.get("x", x))
            y = int(d.get("y", y))
            w = int(d.get("w", w))
            h = int(d.get("h", h))
            zoom = int(d.get("zoom", zoom))
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(max(w, MIN_W), sw)
            h = min(max(h, MIN_H), sh)
            zoom = min(max(zoom, ZOOM_MIN), ZOOM_MAX)
            if not (-40 <= x <= sw - 160 and -10 <= y <= sh - 80):
                x, y = 40, 120
        except Exception:
            pass
        return w, h, x, y, zoom

    def _save_pos(self, *args):
        try:
            STATE_FILE.write_text(
                json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                            "w": self.root.winfo_width(), "h": self.root.winfo_height(),
                            "zoom": self._zoom}),
                encoding="utf-8")
        except Exception:
            pass

    def _zoom_by(self, d):
        self._zoom_set(self._zoom + d)

    def _zoom_set(self, z):
        self._zoom = min(max(int(z), ZOOM_MIN), ZOOM_MAX)
        try:
            self.text.configure(font=(MONO_FAMILY, self._zoom))
            self.text.tag_configure("title2", font=(UI_FAMILY, self._zoom, "bold"))
        except Exception:
            pass
        self._save_pos()

    def _on_zoom_wheel(self, ev):
        self._zoom_by(1 if ev.delta > 0 else -1)

    def _popup(self, ev):
        try:
            self.menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu.grab_release()

    def quit(self):
        self._save_pos()
        log("quit")
        self.root.destroy()

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.pending.clear()
        self.count = 0
        self.fail_count = 0
        self.last_activity = ""
        self._show_empty()
        self._refresh_state()

    def toggle_pause(self):
        self.paused = not self.paused
        self._refresh_state()

    # ---------- 渲染 ----------
    def _insert(self, s, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", s, tag or ())
        self.text.configure(state="disabled")

    def _show_empty(self):
        self._insert("\n")
        self._insert("  等待命令 · 监听中\n\n", "title2")
        self._insert("  只读镜像 · 不执行任何东西 · 不联网\n", "meta")
        if not self.sessions_root.exists():
            self._insert("  注意：找不到会话目录 %s\n" % self.sessions_root, "badc")
            self._insert("  用 --sessions-dir 指定 Codex 日志目录（默认 ~/.codex/sessions）\n", "meta")
        self._insert("  拖动顶部移动 · 拖边缘缩放 · 右键菜单 · Esc 退出\n\n", "meta")

    def _trim(self):
        n = int(self.text.index("end-1c").split(".")[0])
        if n > MAX_KEEP_LINES:
            self.text.configure(state="normal")
            self.text.delete("1.0", "%d.0" % (n - MAX_KEEP_LINES + 400))
            self.text.configure(state="disabled")

    def on_command(self, call_id, cmd, tag):
        if call_id in self.pending:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        first = cmd.strip().splitlines()[0] if cmd.strip() else "(空命令)"
        more = max(0, len(cmd.strip().splitlines()) - 1)
        self.text.configure(state="normal")
        pos = self.text.index("end-1c")
        self.text.insert("end", "▶ ", ("runc",))
        self.text.insert("end", "%s  " % ts, ("meta",))
        self.text.insert("end", first[:160], ("cmd",))
        if more > 0:
            self.text.insert("end", "  (+%d 行)" % more, ("meta",))
        self.text.insert("end", "  %s" % tag, ("faint",))
        self.text.insert("end", "\n")
        mark = "m" + call_id.replace("-", "_")
        self.text.mark_set(mark, "end-1c")
        self.text.configure(state="disabled")
        self.pending[call_id] = {"mark": mark, "pos": pos}
        if tag != self.session_tag:
            self.session_tag = tag
            self.lbl_session.configure(text="只读镜像 · 会话 %s" % tag)
        self.count += 1
        self._refresh_state()

    def on_output(self, call_id, out):
        entry = self.pending.pop(call_id, None)
        out = ANSI_RE.sub("", out or "")
        wm = WALL_RE.search(out)
        ex = EXIT_RE.search(out)
        body = out.split("Output:\n", 1)[1] if "Output:\n" in out else out
        body = body.replace("\r\n", "\n").rstrip()
        lines = [l.rstrip() for l in body.split("\n")] if body.strip() else []
        if len(lines) > BODY_MAX_LINES:
            lines = lines[:BODY_MAX_LINES] + ["…（共 %d 行，已截断）" % len(lines)]
        if not lines:
            lines = ["(无输出)"]
        ok = (ex is None) or (ex.group(1) == "0")
        if ex and ex.group(1) != "0":
            self.fail_count += 1
        self.last_activity = datetime.now().strftime("%H:%M:%S")

        # 把命令前的 ▶ 换成 ✓ / ✗
        if entry:
            try:
                self.text.configure(state="normal")
                self.text.delete(entry["pos"], entry["pos"] + "+1c")
                self.text.insert(entry["pos"], "✓" if ok else "✗", ("okc" if ok else "badc"))
                self.text.configure(state="disabled")
            except Exception:
                pass

        at = entry["mark"] if entry else "end"
        segs = []
        if entry is None:
            segs.append(("···\n", "faint"))
        segs.append(("\n".join(lines) + "\n", None))
        if ex or wm:
            segs.append(("— ", "meta"))
            if ex:
                segs.append((" exit %s " % ex.group(1), "badge_ok" if ok else "badge_bad"))
            if wm:
                segs.append(("  %ss" % wm.group(1), "meta"))
            segs.append(("\n", None))
        segs.append(("\n", None))
        self.text.configure(state="normal")
        for s, tg in segs:
            try:
                self.text.insert(at, s, (tg,) if tg else ())
            except Exception:
                self.text.insert("end", s, (tg,) if tg else ())
        self.text.configure(state="disabled")
        if entry:
            try:
                self.text.mark_unset(entry["mark"])
            except Exception:
                pass
        self.text.see("end")
        self._trim()
        self._refresh_state()

    # ---------- 监听 ----------
    def _dirs(self):
        now = datetime.now()
        for delta in (0, 1, 2):
            d = now - timedelta(days=delta)
            p = self.sessions_root / ("%04d" % d.year) / ("%02d" % d.month) / ("%02d" % d.day)
            if p.is_dir():
                yield p

    def scan(self):
        now = time.time()
        files = []
        for d in self._dirs():
            try:
                for f in d.glob("rollout-*.jsonl"):
                    try:
                        mt = f.stat().st_mtime
                    except OSError:
                        continue
                    key = str(f)
                    if key in self.offsets:
                        if now - mt <= STALE_SECONDS:
                            files.append(f)
                    elif now - mt <= RECENT_SECONDS:
                        files.append(f)
            except OSError:
                continue
        for f in files:
            key = str(f)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if key not in self.offsets:
                self.offsets[key] = size      # 首次见到：只跟进新内容，不回放历史
                self.buffers[key] = ""
                continue
            if size < self.offsets[key]:
                self.offsets[key] = 0
            if size <= self.offsets[key]:
                continue
            try:
                with open(f, "rb") as fh:
                    fh.seek(self.offsets[key])
                    data = fh.read()
                self.offsets[key] += len(data)
            except OSError:
                continue
            buf = self.buffers.get(key, "") + data.decode("utf-8", "replace")
            lines = buf.split("\n")
            self.buffers[key] = lines.pop()
            tag = "#" + f.name[-12:-6]
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                self.handle(obj, tag)

    def handle(self, obj, tag):
        if obj.get("type") != "response_item":
            return
        pl = obj.get("payload", {})
        t = pl.get("type")
        if t == "function_call" and pl.get("name") in EXEC_NAMES:
            cid = pl.get("call_id") or pl.get("id") or ""
            if not cid:
                return
            args = pl.get("arguments") or ""
            cmd = args
            try:
                cmd = json.loads(args).get("cmd", args)
            except Exception:
                pass
            self.on_command(cid, str(cmd), tag)
        elif t == "function_call_output":
            cid = pl.get("call_id") or ""
            if cid:
                self.on_output(cid, pl.get("output") or "")

    def run(self):
        def tick():
            if not self.paused:
                try:
                    self.scan()
                except Exception:
                    log("scan error: " + traceback.format_exc(limit=3))
            self.root.after(POLL_MS, tick)
        self.root.after(200, tick)
        self.root.after(300, self._tick)
        self.root.mainloop()


def main():
    cfg = parse_args()
    try:
        log("boot v%s" % VERSION)
        Mirror(cfg).run()
    except Exception:
        log("fatal: " + traceback.format_exc())


if __name__ == "__main__":
    main()
