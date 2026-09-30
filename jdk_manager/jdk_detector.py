"""JDK 主目录探测。

负责判断一个目录是否为 JDK / JRE 主目录，并解析其版本、厂商、架构信息。

版本信息优先从 JDK 自带的 ``release`` 文件读取（快且准确），
缺失时再回退到执行 ``bin/java.exe -version``。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional
from venv import logger

JAVA_EXE = "java.exe"
JAVAC_EXE = "javac.exe"

# 扫描时跳过的目录名，避免遍历系统无关内容
SKIP_DIR_NAMES = {
    "$recycle.bin",
    "system volume information",
    "windows",
    "winsxs",
    "driverstore",
    "node_modules",
    ".git",
    ".svn",
    ".idea",
    "__pycache__",
    "temp",
    "tmp",
    "recovery",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RELEASE_LINE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$')
_VERSION_IN_OUTPUT = re.compile(r'version\s+"([^"]+)"', re.IGNORECASE)

_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "x86": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
}

# 关键字 -> 规范化厂商名，按优先级从具体到宽泛排列
_VENDOR_KEYWORDS = (
    ("eclipse adoptium", "Eclipse Adoptium"),
    ("temurin", "Eclipse Temurin"),
    ("adoptopenjdk", "AdoptOpenJDK"),
    ("azul", "Azul Zulu"),
    ("zulu", "Azul Zulu"),
    ("amazon", "Amazon Corretto"),
    ("corretto", "Amazon Corretto"),
    ("microsoft", "Microsoft"),
    ("graalvm", "GraalVM"),
    ("liberica", "BellSoft Liberica"),
    ("semeru", "IBM Semeru"),
    ("dragonwell", "Alibaba Dragonwell"),
    ("tencent", "Tencent Kona"),
    ("kona", "Tencent Kona"),
    ("oracle", "Oracle"),
    ("java(tm)", "Oracle"),
    ("openjdk", "OpenJDK"),
)


@dataclass
class JdkInfo:
    """一个 JDK / JRE 主目录的描述信息。"""

    home: str
    version: str = ""
    major: str = ""
    vendor: str = ""
    arch: str = ""
    kind: str = "JDK"       # JDK 或 JRE
    source: str = "manual"  # manual（手动添加）或 scan（扫描发现）
    error: str = ""

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.normpath(self.home))

    @property
    def java_exe(self) -> str:
        return os.path.join(self.home, "bin", JAVA_EXE)

    @property
    def display_version(self) -> str:
        return self.version or "未知"

    @property
    def display_vendor(self) -> str:
        return self.vendor or "未知"

    @property
    def display_arch(self) -> str:
        return self.arch or "-"

    def label(self) -> str:
        """用于列表展示的简短名称，例如 ``JDK 17.0.2 (Eclipse Adoptium)``。"""
        text = f"{self.kind} {self.display_version}"
        if self.vendor:
            text += f" ({self.vendor})"
        return text


def normalize_arch(raw: str) -> str:
    if not raw:
        return ""
    return _ARCH_ALIASES.get(raw.strip().lower(), raw.strip())


def normalize_version(raw: str) -> str:
    """把 ``1.8.0_301`` 这类旧式版本号规范化成 ``8.0_301``。"""
    raw = (raw or "").strip()
    if raw.startswith("1."):
        parts = raw.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            rest = ".".join(parts[2:])
            return parts[1] + (f".{rest}" if rest else "")
    return raw


def major_of(version: str) -> str:
    version = (version or "").strip()
    if not version:
        return ""
    head = re.split(r"[.\-_+]", version, maxsplit=1)[0]
    return head if head.isdigit() else ""


def guess_vendor(text: str) -> str:
    lowered = (text or "").lower()
    for keyword, name in _VENDOR_KEYWORDS:
        if keyword in lowered:
            return name
    return ""


def find_java_home_root(path: str) -> Optional[str]:
    """若给定目录本身包含 ``bin/java.exe`` 则返回它，否则返回 None。"""
    if not path:
        return None
    home = os.path.abspath(path)
    if os.path.isfile(os.path.join(home, "bin", JAVA_EXE)):
        return home
    return None


def _read_release_file(home: str) -> Dict[str, str]:
    """解析 JDK 目录下的 ``release`` 文件，返回键值均为大写键的字典。"""
    path = os.path.join(home, "release")
    if not os.path.isfile(path):
        return {}

    data: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                match = _RELEASE_LINE.match(line)
                if match:
                    data[match.group(1).upper()] = match.group(2).strip()
    except OSError:
        return {}
    return data


def run_java_version(java_exe: str, timeout: int = 15) -> str:
    """执行 ``java -version``，返回合并后的标准输出与标准错误。"""
    try:
        proc = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stderr or "") + (proc.stdout or "")


def detect_home(home: str, source: str = "manual") -> Optional[JdkInfo]:
    """探测指定目录，若不是 JDK/JRE 主目录则返回 None。"""
    root = find_java_home_root(home)
    if root is None:
        return None

    bin_dir = os.path.join(root, "bin")
    kind = "JDK" if os.path.isfile(os.path.join(bin_dir, JAVAC_EXE)) else "JRE"
    info = JdkInfo(home=root, kind=kind, source=source)

    release = _read_release_file(root)
    info.version = normalize_version(release.get("JAVA_VERSION", ""))
    info.vendor = release.get("IMPLEMENTOR", "").strip()
    info.arch = normalize_arch(release.get("OS_ARCH", ""))

    if not info.version or not info.vendor or not info.arch:
        output = run_java_version(os.path.join(bin_dir, JAVA_EXE))
        logger.info(f"java -version 输出: {output}")
        if output:
            if not info.version:
                match = _VERSION_IN_OUTPUT.search(output)
                if match:
                    info.version = normalize_version(match.group(1))
            if not info.vendor:
                info.vendor = guess_vendor(output)
        if not info.version:
            info.error = "无法解析版本信息"

    info.major = major_of(info.version)
    return info


def scan_for_jdks(root: str, max_depth: int = 4) -> List[JdkInfo]:
    """在 ``root`` 下递归查找所有 JDK / JRE 主目录。

    一旦某个目录被识别为 JDK 主目录，就不再深入它的子目录
    （JDK 内部还嵌套着 jre 等目录，无需重复计入）。
    """
    start = os.path.abspath(root)
    results: List[JdkInfo] = []
    seen = set()

    def walk(path: str, depth: int) -> None:
        if find_java_home_root(path) is not None:
            info = detect_home(path, source="scan")
            if info is not None and info.key not in seen:
                seen.add(info.key)
                results.append(info)
            return

        if depth >= max_depth:
            return

        try:
            entries = list(os.scandir(path))
        except (PermissionError, OSError):
            return

        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if name.startswith("$") or name.lower() in SKIP_DIR_NAMES:
                continue
            walk(entry.path, depth + 1)

    walk(start, 0)
    results.sort(key=_sort_key)
    return results


def _sort_key(info: JdkInfo):
    """按主版本号从高到低排序，无法解析版本号的排在最后。"""
    if info.major.isdigit():
        return (0, -int(info.major), info.home.lower())
    return (1, 0, info.home.lower())
