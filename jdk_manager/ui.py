"""Tkinter 主界面。"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Dict, List, Optional, Tuple

from . import downloader, env_manager, store
from .jdk_detector import JdkInfo, detect_home, scan_for_jdks

SCOPE_LABELS = {
    env_manager.SCOPE_USER: "用户变量",
    env_manager.SCOPE_MACHINE: "系统变量",
}

TREE_COLUMNS = ("version", "kind", "path", "current", "vendor", "arch", "source")

TREE_HEADINGS = {
    "current": "当前",
    "version": "版本",
    "kind": "类型",
    "vendor": "厂商",
    "arch": "架构",
    "source": "来源",
    "path": "安装路径",
}

TREE_WIDTHS = {
    "current": 46,
    "version": 96,
    "kind": 56,
    "vendor": 160,
    "arch": 66,
    "source": 60,
    "path": 420,
}


def enable_dpi_awareness() -> None:
    """让进程按物理像素渲染，避免高分屏下被系统拉伸位图导致发虚。

    必须在创建 Tk 窗口之前调用，否则不生效。
    """
    if os.name != "nt":
        return

    import ctypes

    # Win10 1703+：Per-Monitor V2，清晰度最好，也能跟随显示器缩放
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass

    # Win8.1+：Per-Monitor
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass

    # 兜底：系统级 DPI 感知（Win7 起可用）
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def icon_path() -> Optional[str]:
    """定位 app.ico：优先 PyInstaller 解包目录，其次项目根目录。"""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(os.path.join(bundle, "app.ico"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(root, "app.ico"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def center_window(
    window: tk.Misc,
    parent: Optional[tk.Misc] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> None:
    """居中放置窗口：给了父窗口就相对父窗口居中，否则相对屏幕居中。

    用 reqwidth/reqheight 而非实际宽高，因为窗口此时可能尚未映射到屏幕；
    主窗口需要显式传入尺寸，否则请求宽度会大于 geometry 设定值导致偏移。
    """
    window.update_idletasks()
    width = width or window.winfo_reqwidth()
    height = height or window.winfo_reqheight()

    if parent is not None:
        left = parent.winfo_rootx()
        top = parent.winfo_rooty()
        area_w = parent.winfo_width()
        area_h = parent.winfo_height()
    else:
        left = top = 0
        area_w = window.winfo_screenwidth()
        area_h = window.winfo_screenheight()

    x = max(left, left + (area_w - width) // 2)
    y = max(top, top + (area_h - height) // 2)
    window.geometry(f"+{x}+{y}")


class JdkManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._scale = self._apply_dpi_scaling()
        self.title("JDK 版本管理器")
        self.geometry(f"{self.px(750)}x{self.px(300)}")
        self.minsize(self.px(750), self.px(300))
        center_window(self, width=self.px(750), height=self.px(300))
        self._set_window_icon()

        self._jdks: Dict[str, JdkInfo] = {}
        self._order: List[str] = []
        self._busy = False
        self._current_home: Optional[str] = None
        self._events: "queue.Queue[Tuple]" = queue.Queue()

        self._build_widgets()
        self._load_saved()
        self.refresh_current()
        self._populate_tree()

        self.after(80, self._drain_events)
        self.after(150, self._reload_all_async)

    def _set_window_icon(self) -> None:
        """设置窗口图标；用 default 让所有 Toplevel 对话框一并继承。"""
        icon = icon_path()
        if not icon:
            return
        try:
            self.iconbitmap(default=icon)
        except tk.TclError:
            pass

    def _apply_dpi_scaling(self) -> float:
        """读取屏幕 DPI，让 Tk 字体缩放跟随系统，并返回尺寸缩放系数。"""
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except (tk.TclError, ValueError):
            return 1.0
        if dpi <= 0:
            return 1.0
        self.tk.call("tk", "scaling", dpi / 72.0)
        return dpi / 96.0

    def px(self, value: int) -> int:
        """把设计稿像素值换算为当前 DPI 下的实际像素。"""
        return max(1, round(value * self._scale))

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Ok.TLabel", foreground="#0a7d32")
        style.configure("Warn.TLabel", foreground="#b35c00")

        toolbar = ttk.Frame(self, padding=(10, 8, 10, 4))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self._btn_add = ttk.Button(toolbar, text="添加 JDK 目录", command=self._add_jdk)
        self._btn_download = ttk.Button(
            toolbar, text="下载 OpenJDK", command=self._open_download_dialog
        )
        self._btn_scan = ttk.Button(toolbar, text="扫描目录", command=self._scan_directory)
        self._btn_refresh = ttk.Button(toolbar, text="刷新", command=self._reload_all_async)
        self._btn_apply = ttk.Button(toolbar, text="设为当前", command=self._apply_selected)
        self._btn_open = ttk.Button(toolbar, text="打开目录", command=self._open_selected)
        self._btn_remove = ttk.Button(toolbar, text="删除", command=self._remove_selected)
        self._btn_settings = ttk.Button(toolbar, text="设置", command=self._open_settings_dialog)

        for button in (
            self._btn_add,
            self._btn_download,
            self._btn_scan,
            self._btn_refresh,
            self._btn_apply,
            self._btn_open,
            self._btn_remove,
            self._btn_settings,
        ):
            button.pack(side=tk.LEFT, padx=(0, 6))

        self._progress = ttk.Progressbar(toolbar, mode="indeterminate", length=self.px(160))

        info = ttk.LabelFrame(self, text="当前环境", padding=(10, 6))
        info.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(4, 0))

        ttk.Label(info, text="JAVA_HOME").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self._java_home_var = tk.StringVar()
        ttk.Entry(info, textvariable=self._java_home_var, state="readonly").grid(
            row=0, column=1, sticky="ew", pady=2
        )
        self._scope_label = ttk.Label(info, text="")
        self._scope_label.grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(info, text="生效的 java").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        self._java_exe_var = tk.StringVar()
        ttk.Entry(info, textvariable=self._java_exe_var, state="readonly").grid(
            row=1, column=1, sticky="ew", pady=2
        )

        self._warn_label = ttk.Label(
            info, text="", style="Warn.TLabel", wraplength=self.px(900), justify="left"
        )
        self._warn_label.grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 0))
        info.columnconfigure(1, weight=1)

        table_frame = ttk.Frame(self, padding=(10, 8, 10, 4))
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(
            table_frame, columns=TREE_COLUMNS, show="headings", selectmode="browse"
        )
        for column in TREE_COLUMNS:
            self._tree.heading(column, text=TREE_HEADINGS[column])
            self._tree.column(
                column,
                width=self.px(TREE_WIDTHS[column]),
                anchor="center" if column == "current" else "w",
                stretch=False,
            )
        self._tree.tag_configure("current", background="#e3f6e8")
        self._tree.tag_configure("invalid", foreground="#9a9a9a")

        y_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self._tree.yview)
        x_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        # 网格布局：表格左上，纵向滚动条右侧，横向滚动条底部
        self._tree.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self._tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        self._tree.bind("<Double-1>", lambda _event: self._open_selected())

        # 右键上下文菜单：在资源管理器中打开该 JDK 目录
        self._tree_menu = tk.Menu(self, tearoff=0)
        self._tree_menu.add_command(label="打开资源管理器", command=self._open_selected)
        self._tree.bind("<Button-3>", self._on_tree_right_click)

        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(
            self, textvariable=self._status_var, relief=tk.SUNKEN, anchor="w", padding=(8, 3)
        ).pack(side=tk.BOTTOM, fill=tk.X)

        self.bind("<F5>", lambda _event: self._reload_all_async())

    # ------------------------------------------------------------------ #
    # 数据装载与持久化
    # ------------------------------------------------------------------ #

    def _load_saved(self) -> None:
        for home in store.load_jdk_homes():
            info = detect_home(home, source="manual")
            if info is None:
                info = JdkInfo(
                    home=os.path.abspath(home),
                    source="manual",
                    error="目录不存在或不是 JDK 主目录",
                )
            self._store_info(info, persist=False)

    def _store_info(self, info: JdkInfo, persist: bool = True) -> None:
        if info.key not in self._jdks:
            self._order.append(info.key)
        self._jdks[info.key] = info
        if persist:
            self._persist()

    def _persist(self) -> None:
        store.save_jdk_homes([self._jdks[key].home for key in self._order])

    # ------------------------------------------------------------------ #
    # 列表渲染
    # ------------------------------------------------------------------ #

    def _populate_tree(self) -> None:
        selected = self._selected_key()
        current_key = (
            os.path.normcase(os.path.normpath(self._current_home)) if self._current_home else None
        )

        self._tree.delete(*self._tree.get_children())
        for key in self._order:
            info = self._jdks[key]
            is_current = key == current_key
            tags = []
            if is_current:
                tags.append("current")
            if info.error:
                tags.append("invalid")

            self._tree.insert(
                "",
                tk.END,
                iid=key,
                values=(
                    info.display_version,
                    info.kind,
                    info.home,
                    "●" if is_current else "",
                    info.display_vendor,
                    info.display_arch,
                    "扫描" if info.source == "scan" else "手动",
                ),
                tags=tuple(tags),
            )

        if selected and self._tree.exists(selected):
            self._tree.selection_set(selected)
        self._update_buttons()

    def _selected_key(self) -> Optional[str]:
        selection = self._tree.selection()
        return selection[0] if selection else None

    def _selected_info(self) -> Optional[JdkInfo]:
        key = self._selected_key()
        return self._jdks.get(key) if key else None

    def _select_key(self, key: str) -> None:
        if self._tree.exists(key):
            self._tree.selection_set(key)
            self._tree.focus(key)
            self._tree.see(key)

    def _update_buttons(self) -> None:
        idle = tk.NORMAL if not self._busy else tk.DISABLED
        for button in (self._btn_add, self._btn_download, self._btn_scan, self._btn_refresh):
            button.configure(state=idle)
        self._btn_settings.configure(state=idle)

        ready = tk.NORMAL if (self._selected_info() is not None and not self._busy) else tk.DISABLED
        for button in (self._btn_apply, self._btn_open, self._btn_remove):
            button.configure(state=ready)

    def _set_status(self, text: str) -> None:
        self._status_var.set(text)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if busy:
            self._progress.pack(side=tk.LEFT, padx=(12, 0))
            self._progress.start(12)
            if message:
                self._set_status(message)
        else:
            self._progress.stop()
            self._progress.pack_forget()
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 后台任务调度（线程 -> 主线程）
    # ------------------------------------------------------------------ #

    def _drain_events(self) -> None:
        while True:
            try:
                callback, args = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except tk.TclError:
                # 目标窗口可能已经被关闭，跳过这条事件继续处理后面的
                continue
        try:
            self.after(80, self._drain_events)
        except tk.TclError:
            pass

    def _post(self, callback, *args) -> None:
        self._events.put((callback, args))

    # ------------------------------------------------------------------ #
    # 当前环境
    # ------------------------------------------------------------------ #

    def refresh_current(self) -> None:
        value, scope = env_manager.get_java_home()
        self._current_home = value

        if value:
            exists = os.path.isfile(os.path.join(value, "bin", "java.exe"))
            self._java_home_var.set(value if exists else f"{value}    [目录不存在]")
            self._scope_label.configure(
                text=f"（{SCOPE_LABELS.get(scope, scope)}）",
                style="Ok.TLabel" if exists else "Warn.TLabel",
            )
        else:
            self._java_home_var.set("未设置")
            self._scope_label.configure(text="", style="Ok.TLabel")

        effective = env_manager.get_effective_java()
        self._java_exe_var.set(effective or "未在 PATH 中找到 java.exe")

        conflicts = env_manager.machine_conflicts()
        if conflicts:
            self._warn_label.configure(
                text="注意：系统 PATH 中存在 java 条目，其优先级高于用户 PATH，"
                "可能影响切换效果：" + "；".join(conflicts)
            )
        else:
            self._warn_label.configure(text="")

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #

    def _add_jdk(self) -> None:
        home = filedialog.askdirectory(title="选择 JDK 主目录（包含 bin 的那一层）")
        if not home:
            return

        info = detect_home(home, source="manual")
        if info is None:
            messagebox.showwarning(
                "无法识别",
                f"该目录不是有效的 JDK/JRE 主目录：\n{home}\n\n"
                "请选择包含 bin\\java.exe 的根目录。",
            )
            return

        self._store_info(info)
        self._populate_tree()
        self._select_key(info.key)
        self._set_status(f"已添加 {info.label()}")

    def _open_download_dialog(self) -> None:
        if self._busy:
            return
        DownloadDialog(self)

    def _open_settings_dialog(self) -> None:
        if self._busy:
            return
        SettingsDialog(self)

    def download_settings(self) -> Dict[str, str]:
        """合并默认值后的下载设置。"""
        settings = dict(downloader.DEFAULT_SETTINGS)
        settings.update(store.load_settings())
        return settings

    def register_downloaded(self, home: str) -> Optional[JdkInfo]:
        """把刚下载安装好的 JDK 识别并加入列表。"""
        info = detect_home(home, source="manual")
        if info is None:
            messagebox.showwarning(
                "无法识别",
                f"下载已完成，但未能识别为 JDK/JRE：\n{home}",
            )
            return None

        self._store_info(info)
        self._populate_tree()
        self._select_key(info.key)
        self._set_status(f"已添加 {info.label()}")
        return info

    def _scan_directory(self) -> None:
        if self._busy:
            return

        root = filedialog.askdirectory(title="选择要扫描的父目录（例如 D:\\Java）")
        if not root:
            return

        depth = simpledialog.askinteger(
            "扫描深度",
            "向下扫描的目录层数：",
            initialvalue=4,
            minvalue=1,
            maxvalue=8,
            parent=self,
        )
        if depth is None:
            return

        self._set_busy(True, f"正在扫描 {root} ...")
        threading.Thread(target=self._scan_worker, args=(root, depth), daemon=True).start()

    def _scan_worker(self, root: str, depth: int) -> None:
        try:
            found = scan_for_jdks(root, depth)
            error: Optional[BaseException] = None
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            found, error = [], exc
        self._post(self._scan_finished, root, found, error)

    def _scan_finished(self, root: str, found: List[JdkInfo], error) -> None:
        self._set_busy(False)
        if error is not None:
            messagebox.showerror("扫描失败", str(error))
            return

        added = 0
        for info in found:
            existing = self._jdks.get(info.key)
            if existing is not None:
                info.source = existing.source
            else:
                added += 1
                self._order.append(info.key)
            self._jdks[info.key] = info

        if added:
            self._persist()
        self._populate_tree()
        self._set_status(f"扫描完成：发现 {len(found)} 个 JDK/JRE，新增 {added} 个。")

        if not found:
            messagebox.showinfo("扫描完成", f"在 {root} 下未发现 JDK/JRE。")

    def _reload_all_async(self) -> None:
        if self._busy:
            return

        if not self._order:
            self._populate_tree()
            self.refresh_current()
            self._set_status("列表为空，请先添加或扫描 JDK 目录。")
            return

        homes = [self._jdks[key].home for key in self._order]
        self._set_busy(True, "正在重新检测已添加的 JDK ...")
        threading.Thread(target=self._refresh_worker, args=(homes,), daemon=True).start()

    def _refresh_worker(self, homes: List[str]) -> None:
        results = []
        for home in homes:
            info = detect_home(home)
            if info is None:
                info = JdkInfo(
                    home=os.path.abspath(home),
                    error="目录不存在或不是 JDK 主目录",
                )
            results.append(info)
        self._post(self._refresh_finished, results)

    def _refresh_finished(self, results: List[JdkInfo]) -> None:
        for info in results:
            existing = self._jdks.get(info.key)
            if existing is not None:
                info.source = existing.source
            elif info.key not in self._order:
                self._order.append(info.key)
            self._jdks[info.key] = info

        self._set_busy(False)
        self.refresh_current()
        self._populate_tree()
        self._set_status(f"已刷新 {len(results)} 个 JDK。")

    def _apply_selected(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        if info.error:
            messagebox.showwarning("无法切换", f"该 JDK 不可用：{info.error}\n{info.home}")
            return

        plan = env_manager.preview_switch(info.home)
        if not messagebox.askyesno("确认切换", self._format_plan(plan)):
            return

        try:
            env_manager.apply_switch(info.home)
        except PermissionError:
            messagebox.showerror("切换失败", "没有权限修改环境变量，请以管理员身份运行本程序。")
            return
        except OSError as exc:
            messagebox.showerror("切换失败", str(exc))
            return

        self.refresh_current()
        self._populate_tree()
        self._select_key(info.key)
        self._set_status(f"已切换 JAVA_HOME 到 {info.home}")
        messagebox.showinfo(
            "切换成功",
            "JAVA_HOME 与 PATH 已更新，上方「当前生效」已实时反映新状态。\n\n"
            "注意：已经打开的终端、IDE 需要重启后才能读到新的环境变量。",
        )

    @staticmethod
    def _format_plan(plan: env_manager.SwitchPlan) -> str:
        lines = ["将修改用户级环境变量：", ""]
        lines.append("JAVA_HOME：")
        if plan.old_java_home:
            scope = SCOPE_LABELS.get(plan.old_java_home_scope, plan.old_java_home_scope)
            lines.append(f"  {plan.old_java_home}  [{scope}]")
        else:
            lines.append("  （未设置）")
        lines.append(f"  →  {plan.jdk_home}")
        lines.append("")
        lines.append(f"PATH：在最前面加入 {plan.added_entry}")

        if plan.removed_entries:
            lines.append("并移除以下指向其他 JDK 的条目：")
            lines.extend(f"  - {item}" for item in plan.removed_entries)

        if plan.machine_conflicts:
            lines.append("")
            lines.append("警告：系统 PATH 中存在 java 条目，优先级高于用户 PATH，")
            lines.append("可能导致切换不生效（需管理员权限才能清理）：")
            lines.extend(f"  ! {item}" for item in plan.machine_conflicts)

        lines.append("")
        lines.append("是否继续？")
        return "\n".join(lines)

    def _open_selected(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        if not os.path.isdir(info.home):
            messagebox.showwarning("目录不存在", info.home)
            return
        os.startfile(info.home)  # noqa: S606

    def _on_tree_right_click(self, event) -> None:
        """右键选中光标所在行并弹出上下文菜单。"""
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        self._tree.focus(row)
        self._update_buttons()
        try:
            self._tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_menu.grab_release()

    def _remove_selected(self) -> None:
        info = self._selected_info()
        if info is None:
            return

        choice = DeleteDialog.ask(self, info)
        if choice == "list":
            self._remove_from_list(info)
        elif choice == "full":
            self._delete_jdk(info)

    def _remove_from_list(self, info: JdkInfo) -> None:
        if not messagebox.askyesno(
            "从列表删除", f"仅从列表中移除（不删除磁盘文件）：\n{info.home}"
        ):
            return
        self._drop_entry(info.key)
        self._set_status("已从列表移除。")

    def _delete_jdk(self, info: JdkInfo) -> None:
        home = info.home
        if not os.path.isdir(home):
            messagebox.showwarning("目录不存在", f"磁盘上找不到该目录，仅从列表移除：\n{home}")
            self._drop_entry(info.key)
            return

        is_current = bool(
            self._current_home
            and env_manager.same_path(self._current_home, home)
        )
        detail = (
            f"将永久删除磁盘上的 JDK 目录，并从环境变量中移除相关条目：\n\n{home}\n\n"
            "此操作不可撤销，删除前请确认没有程序正在使用该 JDK。"
        )
        if is_current:
            detail += "\n\n注意：该 JDK 是当前 JAVA_HOME 指向的版本，删除后 JAVA_HOME 将被清除。"
        if not messagebox.askyesno("删除 JDK", detail, icon="warning"):
            return

        # 先清理环境变量，再删文件，避免中途失败留下指向空目录的 PATH
        try:
            removed, conflicts = env_manager.remove_jdk_from_env(home)
        except OSError as exc:
            messagebox.showerror("环境变量清理失败", f"{exc}\n\n未删除任何文件。")
            return

        self._set_busy(True, f"正在删除 {home} ...")
        threading.Thread(target=self._delete_worker, args=(home, removed, conflicts), daemon=True).start()

    def _delete_worker(self, home: str, removed: List[str], conflicts: List[str]) -> None:
        error: Optional[BaseException] = None
        try:
            shutil.rmtree(home)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            error = exc
        self._post(self._delete_finished, home, removed, conflicts, error)

    def _delete_finished(
        self, home: str, removed: List[str], conflicts: List[str], error
    ) -> None:
        self._set_busy(False)
        key = os.path.normcase(os.path.normpath(home))
        if error is not None:
            # 环境变量已清理，但文件删除失败——仍从列表移除，并提示用户手动删除
            self._drop_entry(key)
            self.refresh_current()
            messagebox.showerror(
                "删除失败",
                f"环境变量已清理，但删除文件失败：\n{error}\n\n"
                f"请关闭正在使用该 JDK 的程序后，手动删除：\n{home}",
            )
            return

        self._drop_entry(key)
        self.refresh_current()
        msg = "JDK 已从磁盘删除，并从列表与环境变量中移除。"
        if conflicts:
            msg += "\n\n系统级 PATH 仍包含以下条目（需管理员权限手动清理）：\n" + "；".join(conflicts)
        self._set_status("已删除 JDK。")
        messagebox.showinfo("删除完成", msg)

    def _drop_entry(self, key: str) -> None:
        self._jdks.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        self._persist()
        self._populate_tree()


class DeleteDialog(tk.Toplevel):
    """删除方式选择：仅从列表移除，或连同文件与环境变量一起删除。"""

    RESULT_NONE = ""
    RESULT_LIST = "list"
    RESULT_FULL = "full"

    def __init__(self, app: "JdkManagerApp", info: JdkInfo) -> None:
        super().__init__(app)
        self._result = self.RESULT_NONE

        self.title("删除 JDK")
        self.transient(app)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=(14, 12, 14, 10))
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text=f"{info.label()}\n{info.home}",
            justify="left",
            wraplength=app.px(420),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(body, text="请选择删除方式：").pack(anchor="w", pady=(0, 6))

        ttk.Button(body, text="仅从列表删除", command=self._choose_list).pack(
            fill=tk.X, pady=3
        )
        ttk.Label(
            body,
            text="只把该条目从管理列表中移除，磁盘文件与环境变量保持不变。",
            foreground="#555555",
            justify="left",
            wraplength=app.px(420),
        ).pack(anchor="w", pady=(0, 6))

        ttk.Button(
            body, text="删除 JDK 文件并清理环境变量", command=self._choose_full
        ).pack(fill=tk.X, pady=3)
        ttk.Label(
            body,
            text="永久删除该目录下的全部文件，并从 PATH / JAVA_HOME 中移除相关条目（不可撤销）。",
            foreground="#b35c00",
            justify="left",
            wraplength=app.px(420),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Button(body, text="取消", command=self.destroy).pack(anchor="e")

        center_window(self, app)
        self.grab_set()
        self.wait_window()

    def _choose_list(self) -> None:
        self._result = self.RESULT_LIST
        self.destroy()

    def _choose_full(self) -> None:
        self._result = self.RESULT_FULL
        self.destroy()

    @classmethod
    def ask(cls, app: "JdkManagerApp", info: JdkInfo) -> str:
        return cls(app, info)._result


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


class DownloadDialog(tk.Toplevel):
    """从各厂商官方渠道下载并安装指定版本的 OpenJDK。"""

    def __init__(self, app: "JdkManagerApp") -> None:
        super().__init__(app)
        self._app = app
        self._cancel = threading.Event()
        self._running = False
        self._closed = False
        self._step = -1  # 已上报的进度刻度，用于节流

        self.title("下载 OpenJDK")
        self.transient(app)
        self.resizable(False, False)

        self._version_var = tk.StringVar(value="21")
        self._arch_var = tk.StringVar(value=downloader.default_arch())
        self._image_var = tk.StringVar(value=downloader.IMAGE_TYPES[0])
        self._dir_var = tk.StringVar(value=downloader.default_install_root())
        self._status_var = tk.StringVar(value="正在获取可用版本 ...")
        self._progress_var = tk.DoubleVar(value=0.0)

        settings = app.download_settings()
        self._source_id = str(settings.get("source_id", downloader.DEFAULT_SOURCE_ID))
        if self._source_id not in downloader.VENDORS:
            self._source_id = downloader.DEFAULT_SOURCE_ID
        self._source_labels = [downloader.source_label(sid) for sid in downloader.VENDOR_ORDER]
        self._source_var = tk.StringVar(value=downloader.source_label(self._source_id))

        self._build_widgets()
        center_window(self, app)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.grab_set()
        threading.Thread(target=self._versions_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #

    def _build_widgets(self) -> None:
        body = ttk.Frame(self, padding=(12, 10, 12, 10))
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="厂商").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=4)
        self._source_combo = ttk.Combobox(
            body, textvariable=self._source_var, values=self._source_labels,
            state="readonly", width=26,
        )
        self._source_combo.grid(row=0, column=1, columnspan=2, sticky="w", pady=4)
        self._source_combo.bind("<<ComboboxSelected>>", self._on_source_changed)

        ttk.Label(body, text="版本").grid(row=0, column=3, sticky="w", padx=(14, 4), pady=4)
        self._version_combo = ttk.Combobox(
            body,
            textvariable=self._version_var,
            values=[str(item) for item in downloader._FALLBACK_CANDIDATES],
            state="readonly",
            width=8,
        )
        self._version_combo.grid(row=0, column=4, sticky="w", pady=4)

        ttk.Label(body, text="架构").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=4)
        ttk.Combobox(
            body, textvariable=self._arch_var, values=downloader.ARCHES, state="readonly", width=8
        ).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(body, text="类型").grid(row=1, column=2, sticky="w", padx=(14, 4), pady=4)
        self._image_combo = ttk.Combobox(
            body, textvariable=self._image_var, values=downloader.IMAGE_TYPES,
            state="readonly", width=6,
        )
        self._image_combo.grid(row=1, column=3, sticky="w", pady=4)
        self._sync_image_state()

        ttk.Label(body, text="安装目录").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=4)
        ttk.Entry(body, textvariable=self._dir_var).grid(
            row=2, column=1, columnspan=4, sticky="ew", pady=4
        )
        ttk.Button(body, text="浏览", command=self._choose_dir).grid(
            row=2, column=5, sticky="w", padx=(6, 0), pady=4
        )

        self._progress = ttk.Progressbar(
            body,
            mode="determinate",
            maximum=100,
            variable=self._progress_var,
            length=self._app.px(420),
        )
        self._progress.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(10, 4))

        ttk.Label(
            body, textvariable=self._status_var, justify="left", wraplength=self._app.px(420)
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(0, 8))

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=6, sticky="e")
        self._btn_start = ttk.Button(buttons, text="开始下载", command=self._start)
        self._btn_cancel = ttk.Button(
            buttons, text="取消", command=self._cancel_download, state=tk.DISABLED
        )
        self._btn_close = ttk.Button(buttons, text="关闭", command=self._on_close)
        for button in (self._btn_start, self._btn_cancel, self._btn_close):
            button.pack(side=tk.LEFT, padx=(6, 0))

    def _sync_image_state(self) -> None:
        """厂商不提供 JRE 时锁定为 JDK。"""
        if downloader.supports_jre(self._source_id):
            self._image_combo.configure(state="readonly")
        else:
            self._image_var.set(downloader.IMAGE_TYPES[0])
            self._image_combo.configure(state=tk.DISABLED)

    def _on_source_changed(self, _event=None) -> None:
        self._source_id = downloader.VENDOR_ORDER[self._source_labels.index(self._source_var.get())]
        self._sync_image_state()
        self._status_var.set("正在获取可用版本 ...")
        threading.Thread(target=self._versions_worker, daemon=True).start()

    def _choose_dir(self) -> None:
        current = self._dir_var.get().strip()
        chosen = filedialog.askdirectory(
            title="选择安装目录",
            initialdir=current if os.path.isdir(current) else None,
            parent=self,
        )
        if chosen:
            self._dir_var.set(chosen)

    def _update_buttons(self) -> None:
        self._btn_start.configure(state=tk.DISABLED if self._running else tk.NORMAL)
        self._btn_cancel.configure(state=tk.NORMAL if self._running else tk.DISABLED)

    # ------------------------------------------------------------------ #
    # 版本列表
    # ------------------------------------------------------------------ #

    def _versions_worker(self) -> None:
        try:
            versions = downloader.list_versions(self._source_id)
            error: Optional[BaseException] = None
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            versions, error = [], exc
        self._app._post(self._versions_loaded, versions, error)

    def _versions_loaded(self, versions: List[int], error) -> None:
        if self._closed:
            return
        if versions:
            self._version_combo.configure(values=[str(item) for item in versions])
            preview = "、".join(str(item) for item in versions[:6])
            self._status_var.set(f"可用版本（新 → 旧）：{preview} ...")
        else:
            self._status_var.set(f"无法获取在线版本列表（{error}），已使用内置列表。")

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #

    def _start(self) -> None:
        if self._running:
            return

        try:
            feature = int(self._version_var.get())
        except ValueError:
            messagebox.showwarning("版本无效", "请选择有效的版本号。", parent=self)
            return

        install_root = self._dir_var.get().strip()
        if not install_root:
            messagebox.showwarning("目录无效", "请填写安装目录。", parent=self)
            return

        url = downloader.download_url(
            feature,
            self._arch_var.get(),
            self._image_var.get().lower(),
            source_id=self._source_id,
        )

        self._cancel.clear()
        self._running = True
        self._step = -1
        self._progress_var.set(0)
        self._status_var.set(f"正在连接 ...\n{url}")
        self._update_buttons()
        threading.Thread(target=self._worker, args=(url, install_root), daemon=True).start()

    def _worker(self, url: str, install_root: str) -> None:
        try:
            home = downloader.download_and_extract(
                url,
                install_root,
                on_progress=self._on_progress,
                on_status=self._on_status,
                cancel=self._cancel,
            )
            result: Tuple[Optional[str], Optional[BaseException], bool] = (home, None, False)
        except downloader.DownloadCancelled:
            result = (None, None, True)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            result = (None, exc, False)
        self._app._post(self._finish, *result)

    def _on_status(self, text: str) -> None:
        self._app._post(self._set_status, text)

    def _on_progress(self, done: int, total: int) -> None:
        """在工作线程中调用，节流后再投递到主线程。"""
        step = done * 100 // total if total > 0 else done // (5 * 1024 * 1024)
        if step == self._step:
            return
        self._step = step
        self._app._post(self._set_progress, done, total)

    def _set_status(self, text: str) -> None:
        if self._closed:
            return
        self._status_var.set(text)

    def _set_progress(self, done: int, total: int) -> None:
        if self._closed:
            return
        if total > 0:
            self._progress_var.set(done * 100 / total)
            self._status_var.set(
                f"正在下载 ... {done * 100 // total}%"
                f"（{_human_size(done)} / {_human_size(total)}）"
            )
        else:
            self._progress_var.set(0)
            self._status_var.set(f"正在下载 ... {_human_size(done)}")

    def _cancel_download(self) -> None:
        if not self._running:
            return
        self._cancel.set()
        self._btn_cancel.configure(state=tk.DISABLED)
        self._status_var.set("正在取消 ...")

    def _finish(
        self, home: Optional[str], error: Optional[BaseException], cancelled: bool
    ) -> None:
        if self._closed:
            return

        self._running = False
        self._progress_var.set(0)
        self._update_buttons()

        if cancelled:
            self._status_var.set("已取消下载。")
            return

        if error is not None:
            self._status_var.set("下载失败。")
            if isinstance(error, downloader.AlreadyInstalledError):
                messagebox.showinfo(
                    "已存在", f"该版本目录已存在，无需重复下载：\n{error}", parent=self
                )
            else:
                messagebox.showerror("下载失败", str(error), parent=self)
            return

        self._closed = True
        self.destroy()

        info = self._app.register_downloaded(home)
        if info is not None and messagebox.askyesno(
            "下载完成",
            f"{info.label()} 已安装到：\n{home}\n\n是否立即切换为当前 JDK？",
        ):
            self._app._apply_selected()

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "取消下载", "下载尚未完成，确定要取消并关闭吗？", parent=self
            ):
                return
            self._cancel.set()
        self._closed = True
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """配置下载 OpenJDK 的默认官方厂商。"""

    def __init__(self, app: "JdkManagerApp") -> None:
        super().__init__(app)
        self._app = app
        settings = app.download_settings()

        self.title("下载设置")
        self.transient(app)
        self.resizable(False, False)

        self._source_ids = list(downloader.VENDOR_ORDER)
        self._source_labels = [downloader.source_label(sid) for sid in self._source_ids]

        current = (
            self._source_ids.index(settings["source_id"])
            if settings.get("source_id") in self._source_ids
            else 0
        )
        self._source_var = tk.StringVar(value=self._source_labels[current])

        body = ttk.Frame(self, padding=(12, 10, 12, 10))
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="默认厂商").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body, textvariable=self._source_var, values=self._source_labels,
            state="readonly", width=30,
        ).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(
            body,
            text=(
                "下载均走各厂商官方渠道，版本列表实时在线获取。\n"
                "打开「下载 OpenJDK」时默认选中这里设置的厂商。"
            ),
            foreground="#555555",
            justify="left",
            wraplength=self._app.px(400),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 4))

        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(buttons, text="保存", command=self._save).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=(6, 0))

        center_window(self, app)
        self.grab_set()

    def _save(self) -> None:
        source_id = self._source_ids[self._source_labels.index(self._source_var.get())]
        store.save_settings({"source_id": source_id})
        self._app._set_status("下载默认厂商已保存：%s" % downloader.source_label(source_id))
        self.destroy()


def main() -> None:
    enable_dpi_awareness()
    JdkManagerApp().mainloop()
