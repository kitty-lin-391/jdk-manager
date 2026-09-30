"""Windows 环境变量管理（JAVA_HOME / PATH）。

默认操作「用户级」环境变量（``HKCU\\Environment``），因此不需要管理员权限。

直接使用 ``winreg`` 读写注册表，而不是 ``setx`` 或
``[Environment]::SetEnvironmentVariable``，原因有两点：

1. ``setx`` 会把超过 1024 字符的值截断，PATH 很容易超过这个长度；
2. 需要保留 PATH 里 ``%JAVA_HOME%`` 这类未展开的变量引用，
   因此必须原样以 ``REG_EXPAND_SZ`` 写回。
"""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import winreg

JAVA_EXE = "java.exe"

SCOPE_USER = "user"
SCOPE_MACHINE = "machine"

_SCOPE_KEYS = {
    SCOPE_USER: (winreg.HKEY_CURRENT_USER, r"Environment"),
    SCOPE_MACHINE: (
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    ),
}

_WOW64_64KEY = getattr(winreg, "KEY_WOW64_64KEY", 0)

PATH_VAR = "PATH"
JAVA_HOME_VAR = "JAVA_HOME"

JAVA_HOME_BIN = r"%JAVA_HOME%\bin"

_JAVA_HOME_BIN_RE = re.compile(r"^%java_home%[\\/]bin[\\/]?$", re.IGNORECASE)
_JAVAPATH_RE = re.compile(r"common files[\\/]oracle[\\/]java[\\/]javapath", re.IGNORECASE)


@dataclass
class SwitchPlan:
    """一次「切换 JDK」操作的变更明细，用于预览与结果展示。"""

    jdk_home: str
    old_java_home: Optional[str] = None
    old_java_home_scope: Optional[str] = None
    new_path: str = ""
    added_entry: str = JAVA_HOME_BIN
    removed_entries: List[str] = field(default_factory=list)
    machine_conflicts: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #

def read_var(name: str, scope: str = SCOPE_USER) -> Optional[str]:
    """读取环境变量的原始（未展开的）值，不存在时返回 None。"""
    value, _ = read_var_with_type(name, scope)
    return value


def read_var_with_type(name: str, scope: str = SCOPE_USER) -> Tuple[Optional[str], Optional[int]]:
    hive, sub_key = _SCOPE_KEYS[scope]
    try:
        with winreg.OpenKey(hive, sub_key, 0, winreg.KEY_READ | _WOW64_64KEY) as key:
            value, value_type = winreg.QueryValueEx(key, name)
            return value, value_type
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None


def write_var(
    name: str,
    value: str,
    scope: str = SCOPE_USER,
    reg_type: int = winreg.REG_SZ,
) -> None:
    hive, sub_key = _SCOPE_KEYS[scope]
    with winreg.CreateKeyEx(
        hive, sub_key, 0, winreg.KEY_SET_VALUE | _WOW64_64KEY
    ) as key:
        winreg.SetValueEx(key, name, 0, reg_type, value)


def delete_var(name: str, scope: str = SCOPE_USER) -> None:
    hive, sub_key = _SCOPE_KEYS[scope]
    with winreg.OpenKey(
        hive, sub_key, 0, winreg.KEY_SET_VALUE | _WOW64_64KEY
    ) as key:
        winreg.DeleteValue(key, name)


def same_path(left: str, right: str) -> bool:
    """比较两个路径是否指向同一目录（忽略大小写、斜杠与结尾分隔符）。"""
    def normalize(value: str) -> str:
        return os.path.normcase(os.path.normpath(value.rstrip("\\/")))

    return normalize(left) == normalize(right)


# --------------------------------------------------------------------------- #
# JAVA_HOME
# --------------------------------------------------------------------------- #

def get_java_home() -> Tuple[Optional[str], Optional[str]]:
    """返回 ``(JAVA_HOME 值, 所在作用域)``，优先取用户级。"""
    for scope in (SCOPE_USER, SCOPE_MACHINE):
        value = read_var(JAVA_HOME_VAR, scope)
        if value:
            return value.strip().strip('"'), scope
    return None, None


def get_effective_java() -> Optional[str]:
    """按注册表实时解析「新开进程」会用到的 java.exe 路径。

    本进程的 os.environ 是启动时的快照，切换后不会更新，
    因此不能用 shutil.which；改为读取用户级/系统级的 JAVA_HOME 与 PATH，
    按 Windows 合成新进程环境的规则（系统 PATH 在前、用户 PATH 在后）
    逐项展开变量引用并查找 java.exe。
    """
    java_home, _ = get_java_home()
    variables = dict(os.environ)
    if java_home:
        variables["JAVA_HOME"] = java_home

    machine_path = read_var(PATH_VAR, SCOPE_MACHINE) or ""
    user_path = read_var(PATH_VAR, SCOPE_USER) or ""
    for entry in split_path(machine_path) + split_path(user_path):
        directory = _expand_path_entry(entry, variables)
        if directory:
            candidate = os.path.join(directory, JAVA_EXE)
            if os.path.isfile(candidate):
                return candidate
    return None


def _expand_path_entry(entry: str, variables: dict) -> Optional[str]:
    """展开 PATH 单项中的 %VAR% 引用；展开后仍含未解析变量的项返回 None。"""

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        return os.environ.get(name, match.group(0))

    expanded = re.sub(r"%([^%]+)%", replace, entry).strip().strip('"')
    if "%" in expanded:
        return None
    return expanded or None


# --------------------------------------------------------------------------- #
# PATH 处理
# --------------------------------------------------------------------------- #

def split_path(value: str) -> List[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def join_path(entries: List[str]) -> str:
    return ";".join(entries)


def _raw_path_target(entry: str) -> Optional[str]:
    """把 PATH 中的一项展开成磁盘路径；含未知变量或非 bin 目录时返回 None。"""
    expanded = os.path.expandvars(entry).strip().strip('"').rstrip("\\/")
    if not expanded or "%" in expanded or "$" in expanded:
        return None
    return expanded


def is_java_bin_entry(entry: str) -> bool:
    """判断 PATH 中的某一项是否指向某个 JDK/JRE 的 bin 目录。"""
    expanded = _raw_path_target(entry)
    if expanded is None:
        return False
    if os.path.basename(expanded).lower() != "bin":
        return False
    return os.path.isfile(os.path.join(expanded, JAVA_EXE))


def is_javapath_entry(entry: str) -> bool:
    """识别 Oracle 安装器写入的 ``...\\Oracle\\Java\\javapath`` 代理目录。"""
    return bool(_JAVAPATH_RE.search(entry))


def is_java_home_bin_entry(entry: str) -> bool:
    return bool(_JAVA_HOME_BIN_RE.match(entry))


def find_java_entries(value: str, include_java_home_bin: bool = False) -> List[str]:
    """列出 PATH 中所有与 java 相关的条目，用于冲突检测。"""
    found = []
    for entry in split_path(value):
        if is_java_home_bin_entry(entry):
            if include_java_home_bin:
                found.append(entry)
            continue
        if is_javapath_entry(entry) or is_java_bin_entry(entry):
            found.append(entry)
    return found


def machine_conflicts() -> List[str]:
    """系统级 PATH 中与 JDK 相关的条目。

    用户级 PATH 在有效 PATH 中位于系统级之后，因此这些条目会「压住」
    用户级的设置，需要管理员权限才能改动。
    """
    return find_java_entries(read_var(PATH_VAR, SCOPE_MACHINE) or "")


# --------------------------------------------------------------------------- #
# 切换
# --------------------------------------------------------------------------- #

def build_new_path(current_path: str) -> Tuple[str, List[str]]:
    """基于现有用户级 PATH 生成切换后的 PATH。

    规则：移除所有指向具体 JDK 的 bin 目录与 Oracle javapath 代理目录，
    再把 ``%JAVA_HOME%\\bin`` 统一放到最前面，让当前选中的 JDK 优先生效。
    """
    kept: List[str] = []
    removed: List[str] = []

    for entry in split_path(current_path):
        if is_java_home_bin_entry(entry):
            continue  # 稍后统一插到最前面
        if is_javapath_entry(entry) or is_java_bin_entry(entry):
            removed.append(entry)
            continue
        kept.append(entry)

    new_path = join_path([JAVA_HOME_BIN] + kept)
    return new_path, removed


def preview_switch(jdk_home: str) -> SwitchPlan:
    """生成切换预览，不写入任何内容。"""
    old_home, old_scope = get_java_home()
    current_path = read_var(PATH_VAR, SCOPE_USER) or ""
    new_path, removed = build_new_path(current_path)

    return SwitchPlan(
        jdk_home=os.path.abspath(jdk_home),
        old_java_home=old_home,
        old_java_home_scope=old_scope,
        new_path=new_path,
        removed_entries=removed,
        machine_conflicts=machine_conflicts(),
    )


def apply_switch(jdk_home: str) -> SwitchPlan:
    """把 JAVA_HOME 指向指定 JDK，并修正用户级 PATH。

    只改用户级变量，因此无需管理员权限。
    """
    plan = preview_switch(jdk_home)

    write_var(JAVA_HOME_VAR, plan.jdk_home, SCOPE_USER, winreg.REG_SZ)
    write_var(PATH_VAR, plan.new_path, SCOPE_USER, winreg.REG_EXPAND_SZ)
    broadcast_change()
    return plan


def remove_jdk_from_env(jdk_home: str) -> Tuple[List[str], List[str]]:
    """从环境变量中清理指向 ``jdk_home`` 的条目。

    处理内容：
    1. 用户级 PATH 中指向该 JDK ``bin`` 的条目（直接写回，不再插入
       ``%JAVA_HOME%\\bin``，避免该 JDK 被删除后 PATH 仍指向空目录）；
    2. 用户级 JAVA_HOME 若正指向该 JDK，则删除该变量。

    返回 ``(已移除的 PATH 条目, 系统级冲突条目)``。系统级变量需要管理员
    权限，这里只报告不修改。
    """
    target_bin = os.path.join(jdk_home, "bin")
    current_path = read_var(PATH_VAR, SCOPE_USER) or ""

    kept: List[str] = []
    removed: List[str] = []
    for entry in split_path(current_path):
        expanded = _raw_path_target(entry)
        if expanded and same_path(expanded, target_bin):
            removed.append(entry)
            continue
        kept.append(entry)

    if removed:
        write_var(PATH_VAR, join_path(kept), SCOPE_USER, winreg.REG_EXPAND_SZ)

    home_value, home_scope = get_java_home()
    if home_value and same_path(home_value, jdk_home):
        if home_scope == SCOPE_USER:
            try:
                delete_var(JAVA_HOME_VAR, SCOPE_USER)
            except OSError:
                pass
        else:
            removed.append(f"JAVA_HOME={home_value}（系统级）")

    broadcast_change()
    return removed, machine_conflicts()


def broadcast_change() -> None:
    """通知资源管理器环境变量已更新，使新开的进程能读到新值。"""
    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    result = ctypes.c_ulong()

    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_settingchange,
            0,
            ctypes.c_wchar_p("Environment"),
            smto_abortifhung,
            5000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass
