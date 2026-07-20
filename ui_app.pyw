"""
ui_app.py
QP Copilot 桌面 UI（Tkinter）。

只负责界面展示和用户输入/确认，不重新实现任何业务逻辑：
  - "Start Inspection" 实际上是启动子进程运行 main.py（跟在终端里手动运行完全一样，
    只是把 scene_id / frame_count / 确认回车 这几个 input() 提示通过 stdin 自动喂过去），
    再把 main.py 的标准输出实时解析出来（[进度] 第 X/Y 帧）显示进度条和状态。
  - "Delete Scene Files" 同理，是启动子进程运行 cleanup_scene.py，把 scene_id 和二次确认
    的 "DELETE" 通过 stdin 喂过去，重用 cleanup_scene.py 已有的匹配/删除逻辑。
  - Rule Engine / 采集 / Report 生成的具体实现全部在 main.py 以及它 import 的模块里，
    这个文件完全不碰、不 import 它们的内部函数，只当成两个可执行脚本来调用。

运行前提跟直接跑 main.py 一样：Chrome 需要已经按 README 启动调试端口、手动登录 QP 平台、
停在目标 scene 的第 1 帧，再点 Start Inspection。

后续要加 "AI Review" 按钮，只需要在 _build_ui() 里加一个 Button，绑定一个新的
_on_ai_review() 方法（同样是起子进程调用已有脚本），不需要动这个文件里其它任何逻辑。
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "outputs" / "reports"
MAIN_SCRIPT = BASE_DIR / "main.py"
CLEANUP_SCRIPT = BASE_DIR / "cleanup_scene.py"

# 子进程不新开控制台窗口（Windows 下 pythonw.exe 启动本 App 时，Popen 默认会给
# 控制台子系统的子进程新分配一个黑窗口，这里禁用掉）
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 子进程的 stdout 强制按 UTF-8 编码：Windows 下 python 子进程默认按控制台代码页
# （通常是 GBK）编码输出，而这里用 encoding="utf-8" 解码，编码不一致会导致
# 中文（包括 "[进度]"）解码成 � 乱码，进度正则永远匹配不上、进度条卡在 0/0。
_CHILD_ENV = os.environ.copy()
_CHILD_ENV["PYTHONIOENCODING"] = "utf-8"

DEFAULT_FRAME_COUNT = 81

PROGRESS_RE = re.compile(r"\[进度\]\s*第\s*(\d+)/(\d+)\s*帧")

# (显示名, 文件名) —— 都在 outputs/reports/ 下，跟 main.py 生成的文件名保持一致
REPORT_FILES = [
    "bbox_data.csv",
    "rule_report.csv",
    "rule_summary.json",
]


def _load_default_frame_count() -> int:
    """读 config.yaml 里的默认帧数，读不到就用 81（跟 main.py 的默认值保持一致）。"""
    try:
        import yaml

        with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return int(config["scene"].get("frame_count", DEFAULT_FRAME_COUNT))
    except Exception:
        return DEFAULT_FRAME_COUNT


def _open_path(path: Path) -> None:
    """用系统默认程序打开文件/文件夹（跨平台：Windows / macOS / Linux）。"""
    if not path.exists():
        messagebox.showerror("Open failed", f"文件不存在:\n{path}")
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        messagebox.showerror("Open failed", f"无法打开:\n{path}\n\n{exc}")


class DeleteConfirmDialog(tk.Toplevel):
    """Cleanup 二次确认弹窗：必须手动输入 DELETE 才会返回 confirmed=True。"""

    def __init__(self, parent: tk.Tk, scene_id: str):
        super().__init__(parent)
        self.title("Confirm Delete")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.confirmed = False

        tk.Label(
            self,
            text=f"Delete ALL files related to this Scene?\n\nScene Number: {scene_id}",
            justify="center",
            padx=24,
            pady=12,
        ).pack()
        tk.Label(self, text='Type "DELETE" to confirm:').pack(pady=(0, 4))

        self.entry = tk.Entry(self, justify="center")
        self.entry.pack(padx=24, pady=4, fill="x")
        self.entry.focus_set()

        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=12)
        tk.Button(btn_frame, text="Cancel", width=10, command=self._cancel).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Confirm", width=10, command=self._confirm).pack(side="left", padx=6)

        self.bind("<Return>", lambda _e: self._confirm())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _confirm(self) -> None:
        if self.entry.get().strip() == "DELETE":
            self.confirmed = True
            self.destroy()
        else:
            messagebox.showwarning(
                "Not confirmed", 'Please type "DELETE" exactly to proceed.', parent=self
            )

    def _cancel(self) -> None:
        self.confirmed = False
        self.destroy()


class QPCopilotApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QP Copilot")
        self.root.resizable(False, False)

        self.event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.inspection_running = False
        self.cleanup_running = False
        self.report_rows: dict[str, tuple[tk.Label, tk.Button]] = {}

        self._build_ui()
        self.root.after(100, self._poll_queue)

    # ---------------------------------------------------------- UI 构建 ----
    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 6}

        tk.Label(self.root, text="QP Copilot", font=("Segoe UI", 18, "bold")).pack(pady=(16, 8))

        form = tk.Frame(self.root)
        form.pack(fill="x", **pad)

        tk.Label(form, text="Scene Number:").grid(row=0, column=0, sticky="w")
        self.scene_entry = tk.Entry(form, width=30)
        self.scene_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))

        tk.Label(form, text="Frame Count:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.frame_count_entry = tk.Entry(form, width=10)
        self.frame_count_entry.insert(0, str(_load_default_frame_count()))
        self.frame_count_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        self.start_btn = tk.Button(
            self.root, text="Start Inspection", width=24, command=self._on_start_inspection
        )
        self.start_btn.pack(pady=10)

        progress_frame = tk.Frame(self.root)
        progress_frame.pack(fill="x", **pad)
        tk.Label(progress_frame, text="Progress").pack(anchor="w")
        self.progress_bar = ttk.Progressbar(progress_frame, length=320, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(4, 2))
        self.progress_label = tk.Label(progress_frame, text="0 / 0")
        self.progress_label.pack(anchor="w")

        self.status_label = tk.Label(self.root, text="Status: Waiting...", fg="#555555")
        self.status_label.pack(pady=(4, 10))

        self.report_frame = tk.Frame(self.root)
        self.report_frame.pack(fill="x", **pad)
        for filename in REPORT_FILES:
            row = tk.Frame(self.report_frame)
            row.pack(fill="x", pady=2)
            status_lbl = tk.Label(row, text=f"  {filename}", anchor="w", width=22)
            status_lbl.pack(side="left")
            open_btn = tk.Button(
                row,
                text="Open",
                width=8,
                state="disabled",
                command=lambda f=filename: _open_path(REPORTS_DIR / f),
            )
            open_btn.pack(side="left")
            self.report_rows[filename] = (status_lbl, open_btn)

        tk.Button(
            self.root,
            text="Open Report Folder",
            width=24,
            command=lambda: _open_path(REPORTS_DIR),
        ).pack(pady=(4, 12))

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=16)

        tk.Label(self.root, text="Cleanup Scene", font=("Segoe UI", 12, "bold")).pack(pady=(12, 6))

        cleanup_form = tk.Frame(self.root)
        cleanup_form.pack(fill="x", **pad)
        tk.Label(cleanup_form, text="Scene Number:").grid(row=0, column=0, sticky="w")
        self.cleanup_scene_entry = tk.Entry(cleanup_form, width=30)
        self.cleanup_scene_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.cleanup_btn = tk.Button(
            self.root, text="Delete Scene Files", width=24, command=self._on_delete_scene_files
        )
        self.cleanup_btn.pack(pady=(6, 16))

    # ---------------------------------------------------- Start Inspection ----
    def _on_start_inspection(self) -> None:
        if self.inspection_running:
            return

        scene_id = self.scene_entry.get().strip()
        frame_count_raw = self.frame_count_entry.get().strip()

        for filename, (status_lbl, open_btn) in self.report_rows.items():
            status_lbl.config(text=f"  {filename}", fg="black")
            open_btn.config(state="disabled")

        self.inspection_running = True
        self.start_btn.config(state="disabled")
        self.progress_bar.config(value=0, maximum=100)
        self.progress_label.config(text="0 / 0")
        self.status_label.config(text="Status: Running...", fg="#008800")

        thread = threading.Thread(
            target=self._run_main_process, args=(scene_id, frame_count_raw), daemon=True
        )
        thread.start()

    def _run_main_process(self, scene_id: str, frame_count_raw: str) -> None:
        # main.py 依次用 input() 问：scene_id -> frame_count -> 确认回车（无内容），
        # 三行喂给 stdin，跟人在终端里敲的效果完全一样。
        stdin_payload = f"{scene_id}\n{frame_count_raw}\n\n"
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", str(MAIN_SCRIPT)],
                cwd=str(BASE_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
                env=_CHILD_ENV,
            )
        except OSError as exc:
            self.event_queue.put(("error", f"启动 main.py 失败: {exc}"))
            return

        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(stdin_payload)
        process.stdin.close()

        for line in process.stdout:
            match = PROGRESS_RE.search(line)
            if match:
                current, total = int(match.group(1)), int(match.group(2))
                self.event_queue.put(("progress", (current, total)))

        returncode = process.wait()
        if returncode == 0:
            self.event_queue.put(("done", None))
        else:
            self.event_queue.put(("error", f"main.py 退出码 {returncode}（详情请看终端/日志）"))

    # ------------------------------------------------------------- Cleanup ----
    def _on_delete_scene_files(self) -> None:
        if self.cleanup_running:
            return

        scene_id = self.cleanup_scene_entry.get().strip()
        if not scene_id:
            messagebox.showwarning("Scene Number required", "请输入 Scene Number。")
            return

        dialog = DeleteConfirmDialog(self.root, scene_id)
        self.root.wait_window(dialog)
        if not dialog.confirmed:
            return

        self.cleanup_running = True
        self.cleanup_btn.config(state="disabled")
        self.status_label.config(text="Status: Running...", fg="#008800")

        thread = threading.Thread(target=self._run_cleanup_process, args=(scene_id,), daemon=True)
        thread.start()

    def _run_cleanup_process(self, scene_id: str) -> None:
        # cleanup_scene.py 依次问：scene_id -> 确认输入 DELETE。UI 这边已经弹窗确认过一次，
        # 这里把 "DELETE" 一起喂进去，触发它自己已有的二次确认+删除逻辑。
        stdin_payload = f"{scene_id}\nDELETE\n"
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(CLEANUP_SCRIPT)],
                cwd=str(BASE_DIR),
                input=stdin_payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
                env=_CHILD_ENV,
            )
        except OSError as exc:
            self.event_queue.put(("cleanup_error", f"启动 cleanup_scene.py 失败: {exc}"))
            return

        if result.returncode == 0:
            self.event_queue.put(("cleanup_done", result.stdout))
        else:
            self.event_queue.put(("cleanup_error", result.stdout or f"退出码 {result.returncode}"))

    # --------------------------------------------------------------- 队列轮询 ----
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "progress":
                    current, total = payload
                    self.progress_bar.config(value=current, maximum=max(total, 1))
                    self.progress_label.config(text=f"{current} / {total}")
                elif kind == "done":
                    self.status_label.config(text="Status: Completed.", fg="#008800")
                    self.start_btn.config(state="normal")
                    self.inspection_running = False
                    self._refresh_report_files()
                elif kind == "error":
                    self.status_label.config(text="Status: Error", fg="#cc0000")
                    self.start_btn.config(state="normal")
                    self.inspection_running = False
                    messagebox.showerror("Inspection failed", str(payload))
                elif kind == "cleanup_done":
                    self.status_label.config(text="Status: Waiting...", fg="#555555")
                    self.cleanup_btn.config(state="normal")
                    self.cleanup_running = False
                    messagebox.showinfo("Cleanup", "Cleanup completed.")
                elif kind == "cleanup_error":
                    self.status_label.config(text="Status: Waiting...", fg="#555555")
                    self.cleanup_btn.config(state="normal")
                    self.cleanup_running = False
                    messagebox.showerror("Cleanup failed", str(payload))
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_queue)

    def _refresh_report_files(self) -> None:
        for filename, (status_lbl, open_btn) in self.report_rows.items():
            path = REPORTS_DIR / filename
            if path.is_file():
                status_lbl.config(text=f"✓ {filename}", fg="#008800")
                open_btn.config(state="normal")
            else:
                status_lbl.config(text=f"  {filename} (not found)", fg="#cc0000")
                open_btn.config(state="disabled")


def main() -> None:
    root = tk.Tk()
    QPCopilotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
