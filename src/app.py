"""PagesLinker GUI.

Drag folders/zips/files in, configure, click build. The engine lives in
pageslinker.py (pure Python, no GUI deps). tkinterdnd2 is optional: if it is
missing we fall back to "click to select" and "drop files onto the EXE icon"
(argv pickup), which is why this file does not hard-fail without it.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from pageslinker import (  # noqa: E402
    MAX_ASSET_BYTES, MAX_FILES, PagesLinkerError, StatusEvent, build_site, human_size,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # noqa: F401
    HAS_DND = True
except Exception:
    HAS_DND = False

APP_TITLE = "PagesLinker · Cloudflare Pages 资源直链打包器"
MI = 1024 * 1024


class App:
    def __init__(self, root) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.geometry("860x640")

        self.inputs: list[str] = []
        self._busy = False
        self._cancel = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self.last_outdir: str = ""

        self._build_ui()
        self.root.after(120, self._poll)

        # pick up paths dropped onto the EXE icon (argv)
        for a in sys.argv[1:]:
            p = os.path.abspath(a)
            if os.path.exists(p) and p not in self.inputs:
                self.inputs.append(p)
        if self.inputs:
            self._refresh_list()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="拖入 文件 / 文件夹 / zip（可多个混合）").pack(anchor="w")
        self.drop = tk.Text(top, height=4, wrap="word")
        self.drop.pack(fill="x", pady=(4, 6))
        self.drop.insert("1.0", "（拖拽区 / 点击下方按钮选择）")
        self.drop.configure(state="disabled")

        btnrow = ttk.Frame(top)
        btnrow.pack(fill="x")
        ttk.Button(btnrow, text="添加文件…", command=self._pick_files).pack(side="left")
        ttk.Button(btnrow, text="添加文件夹…", command=self._pick_dir).pack(side="left")
        ttk.Button(btnrow, text="清空", command=self._clear).pack(side="left", padx=(8, 0))
        ttk.Button(btnrow, text="打开输出目录", command=self._open_outdir).pack(side="right")

        opts = ttk.LabelFrame(self.root, text="参数", padding=8)
        opts.pack(fill="x", padx=10, pady=(4, 0))

        row1 = ttk.Frame(opts)
        row1.pack(fill="x")
        ttk.Label(row1, text="分片阈值 (MiB, 1–24):").pack(side="left")
        self.thr = tk.StringVar(value="20")
        ttk.Spinbox(row1, from_=1, to=24, textvariable=self.thr, width=8).pack(side="left", padx=(4, 16))
        self.var_dedupe = tk.BooleanVar(value=True)
        self.var_hidden = tk.BooleanVar(value=True)
        self.var_ascii = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="内容去重", variable=self.var_dedupe).pack(side="left", padx=6)
        ttk.Checkbutton(row1, text="跳过隐藏文件", variable=self.var_hidden).pack(side="left", padx=6)
        ttk.Checkbutton(row1, text="全量 ASCII 安全化重命名", variable=self.var_ascii).pack(side="left", padx=6)

        row2 = ttk.Frame(opts)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="输出目录:").pack(side="left")
        self.outvar = tk.StringVar(value=str(Path.cwd() / "output"))
        ttk.Entry(row2, textvariable=self.outvar).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row2, text="选择…", command=self._pick_outdir).pack(side="left")

        buildrow = ttk.Frame(self.root, padding=10)
        buildrow.pack(fill="x")
        self.build_btn = ttk.Button(buildrow, text="开始构建", command=self._build)
        self.build_btn.pack(side="left")
        self.cancel_btn = ttk.Button(buildrow, text="取消", command=self._cancel_build, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.pbar = ttk.Progressbar(buildrow, mode="determinate")
        self.pbar.pack(side="left", fill="x", expand=True, padx=(16, 0))

        mid = ttk.LabelFrame(self.root, text="文件列表", padding=6)
        mid.pack(fill="both", expand=True, padx=10, pady=(0, 0))
        cols = ("name", "size", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="名称")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=420)
        self.tree.column("size", width=120)
        self.tree.column("status", width=160)
        self.tree.pack(fill="both", expand=True)

        logf = ttk.LabelFrame(self.root, text="日志", padding=6)
        logf.pack(fill="x", padx=10, pady=(6, 10))
        self.log = scrolledtext.ScrolledText(logf, height=8, state="disabled")
        self.log.pack(fill="x")

        if HAS_DND:
            try:
                self.drop.drop_target_register(DND_FILES)
                self.drop.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    # ------------------------------------------------------------- helpers
    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for p in self.inputs:
            path = Path(p)
            if path.is_dir():
                n = 0
                for _r, _d, fs in os.walk(path):
                    n += len(fs)
                size = "文件夹"
            else:
                size = human_size(path.stat().st_size)
            self.tree.insert("", "end", values=(p, size, "待处理"))
        txt = "\n".join(self.inputs) if self.inputs else "（拖拽区 / 点击下方按钮选择）"
        self.drop.configure(state="normal")
        self.drop.delete("1.0", "end")
        self.drop.insert("1.0", txt)
        self.drop.configure(state="disabled")

    def _add(self, paths: list[str]) -> None:
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.exists(ap) and ap not in self.inputs:
                self.inputs.append(ap)
        self._refresh_list()

    def _pick_files(self) -> None:
        fs = filedialog.askopenfilenames(title="选择文件或 zip")
        if fs:
            self._add(list(fs))

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self._add([d])

    def _pick_outdir(self) -> None:
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.outvar.set(d)

    def _clear(self) -> None:
        self.inputs.clear()
        self._refresh_list()

    def _open_outdir(self) -> None:
        d = self.last_outdir or self.outvar.get()
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # type: ignore[attr-defined]
            except Exception:
                subprocess.Popen(["explorer", d])
        elif d:
            messagebox.showinfo("提示", "输出目录尚不存在，请先构建。")

    def _on_drop(self, event) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
            self._add(paths)
        except Exception as e:
            self._log(f"拖拽解析失败: {e}")

    # ------------------------------------------------------------- build
    def _build(self) -> None:
        if self._busy:
            return
        if not self.inputs:
            messagebox.showwarning("提示", "请先拖入或选择至少一个文件/文件夹/zip。")
            return
        try:
            thr = float(self.thr.get())
        except ValueError:
            messagebox.showerror("错误", "分片阈值必须是数字。")
            return
        if not (1 <= thr <= 24):
            messagebox.showerror("错误", "分片阈值必须在 1–24 MiB 之间。")
            return

        outdir = self.outvar.get().strip() or str(Path.cwd() / "output")
        site = Path(outdir) / "_site"
        if site.exists() and any(site.iterdir()):
            if not messagebox.askyesno("输出目录非空", "输出目录已存在且非空，是否清空重建？\n（选“否”将取消）"):
                return

        self._busy = True
        self._cancel.clear()
        self.build_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.pbar.configure(value=0, maximum=1)
        self._log("开始构建…")

        def emit(ev: StatusEvent) -> None:
            self._q.put(ev)

        def worker() -> None:
            try:
                res = build_site(
                    self.inputs, outdir,
                    threshold_mb=thr,
                    dedupe=self.var_dedupe.get(),
                    skip_hidden=self.var_hidden.get(),
                    force_ascii=self.var_ascii.get(),
                    emit=emit,
                    cancel=self._cancel.is_set,
                )
                self._q.put(("result", res))
            except PagesLinkerError as e:
                self._q.put(("error", str(e)))
            except Exception as e:  # noqa: BLE001
                self._q.put(("error", f"未预期错误: {e}"))
            finally:
                self._q.put(("finish", None))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_build(self) -> None:
        if self._busy:
            self._cancel.set()
            self._log("正在取消…")

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                if isinstance(msg, StatusEvent):
                    if msg.kind == "log":
                        self._log("  " + msg.message)
                    elif msg.kind == "file":
                        self.pbar.configure(maximum=max(1, msg.total), value=msg.index)
                        self._log(msg.path)
                elif isinstance(msg, tuple):
                    kind, payload = msg
                    if kind == "result":
                        self._on_result(payload)
                    elif kind == "error":
                        self._log("错误: " + payload)
                        messagebox.showerror("构建失败", payload)
                    elif kind == "finish":
                        self._busy = False
                        self.build_btn.configure(state="normal")
                        self.cancel_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_result(self, res) -> None:
        self.last_outdir = str(Path(res.zip_path).parent)
        self.pbar.configure(value=self.pbar["maximum"])
        self._log("=== 构建完成 ===")
        self._log(f"站点目录: {res.site_dir}")
        self._log(f"ZIP:       {res.zip_path}")
        self._log(f"资源: {len(res.entries)} (直接 {res.direct_count} / 分片 {res.chunked_count})")
        self._log(f"分片: {res.chunk_count}  站点文件: {res.total_files}  原始体积: {human_size(res.total_bytes)}")
        self._log("部署后直链 = https://<项目名>.pages.dev/<资源路径>")
        messagebox.showinfo(
            "构建完成",
            f"资源 {len(res.entries)} 个\n分片 {res.chunk_count} 片\n站点文件 {res.total_files} 个\n\n"
            f"输出目录：\n{res.site_dir}",
        )
        try:
            os.startfile(str(Path(res.zip_path).parent))  # type: ignore[attr-defined]
        except Exception:
            pass


def main() -> int:
    # Smoke-test hook for CI: verify the frozen EXE boots far enough to
    # import everything and touch the GUI layer without staying alive.
    if "--selftest-smoke" in sys.argv:
        try:
            root = tk.Tk()
            root.withdraw()
            if HAS_DND:
                root.destroy()
                root = TkinterDnD.Tk()
                root.withdraw()
            app = App(root)
            root.update_idletasks()
            root.destroy()
            print("SMOKE OK")
            return 0
        except Exception as e:
            print(f"SMOKE FAIL: {e}")
            return 1

    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
