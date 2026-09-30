"""从各厂商官方渠道下载免安装版 OpenJDK。

只依赖标准库。每个厂商都实现了：

* ``list_versions``  —— 动态查询该厂商当前提供的主版本号（不硬编码）；
* ``download_url``   —— 构造免安装 zip 的官方下载地址。

下载的都是 zip 压缩包，解压到用户目录即可使用，无需管理员权限。
"""

from __future__ import annotations

import json
import os
import platform
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

TEMURIN_API = "https://api.adoptium.net/v3"

ARCHES = ("x64", "x86", "aarch64")
IMAGE_TYPES = ("JDK", "JRE")

_TIMEOUT = 30
_CHUNK = 256 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# 厂商注册表
# --------------------------------------------------------------------------- #

SOURCE_TEMURIN = "temurin"
SOURCE_CORRETTO = "corretto"
SOURCE_MICROSOFT = "microsoft"
SOURCE_AZUL = "azul"
SOURCE_LIBERICA = "liberica"

#: 每个厂商的能力描述：显示名、是否提供 JRE、版本发现方式
VENDORS: Dict[str, Dict[str, object]] = {
    SOURCE_TEMURIN: {"label": "Eclipse Temurin（Adoptium）", "jre": True},
    SOURCE_CORRETTO: {"label": "Amazon Corretto", "jre": True},
    SOURCE_MICROSOFT: {"label": "Microsoft Build of OpenJDK", "jre": False},
    SOURCE_AZUL: {"label": "Azul Zulu", "jre": True},
    SOURCE_LIBERICA: {"label": "BellSoft Liberica", "jre": True},
}

#: 下拉框展示顺序
VENDOR_ORDER: Tuple[str, ...] = (
    SOURCE_TEMURIN,
    SOURCE_CORRETTO,
    SOURCE_MICROSOFT,
    SOURCE_AZUL,
    SOURCE_LIBERICA,
)

DEFAULT_SOURCE_ID = SOURCE_TEMURIN

#: 下载设置默认值（全部走官方，无镜像项）
DEFAULT_SETTINGS = {"source_id": DEFAULT_SOURCE_ID}

#: 版本发现时探测的候选主版本号上界（Temurin 接口不可用时的兜底窗口）
_FALLBACK_CANDIDATES = tuple(range(8, 28))


class DownloadError(Exception):
    """下载或解压失败。"""


class DownloadCancelled(DownloadError):
    """用户主动取消。"""


class AlreadyInstalledError(DownloadError):
    """目标目录已存在，无需重复下载。"""


#: 进度回调：(已下载字节, 总字节)，总字节为 0 表示服务端未返回长度
ProgressCallback = Callable[[int, int], None]
#: 状态回调：用于提示「正在下载」「正在解压」等阶段变化
StatusCallback = Callable[[str], None]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def default_install_root() -> str:
    """默认安装位置：%LOCALAPPDATA%\\Programs\\Java。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", "Java")


def default_arch() -> str:
    """按当前系统推断架构。"""
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return "x64"


def source_label(source_id: str) -> str:
    vendor = VENDORS.get(source_id)
    return str(vendor["label"]) if vendor else source_id


def supports_jre(source_id: str) -> bool:
    vendor = VENDORS.get(source_id)
    return bool(vendor and vendor.get("jre"))


def _open(url: str, timeout: int = _TIMEOUT, method: Optional[str] = None,
          insecure: bool = False):
    """打开 URL，统一转成 DownloadError。insecure 用于证书链有问题的站点。"""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method=method)
    context = _insecure_context() if insecure else None
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=context)
    except Exception as exc:  # noqa: BLE001 - 统一转成 DownloadError 便于界面展示
        raise DownloadError(str(exc)) from exc


def _insecure_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _open_json(url: str, insecure: bool = False):
    with _open(url, insecure=insecure) as response:
        return json.load(response)


def _candidate_majors() -> List[int]:
    """版本发现用的候选主版本号：优先取 Temurin 全集，失败则用兜底窗口。"""
    try:
        return _temurin_versions()
    except DownloadError:
        return list(_FALLBACK_CANDIDATES)


# --------------------------------------------------------------------------- #
# 版本发现（每个厂商动态查询，不硬编码）
# --------------------------------------------------------------------------- #

def list_versions(source_id: str = DEFAULT_SOURCE_ID) -> List[int]:
    """查询指定厂商当前可用的主版本号（降序）。"""
    if source_id == SOURCE_TEMURIN:
        return _temurin_versions()
    if source_id == SOURCE_AZUL:
        return _azul_versions()
    if source_id == SOURCE_LIBERICA:
        return _liberica_versions()
    if source_id == SOURCE_CORRETTO:
        return _probe_versions(_corretto_probe_url)
    if source_id == SOURCE_MICROSOFT:
        return _probe_versions(_microsoft_probe_url)
    raise DownloadError(f"未知的下载源：{source_id}")


def _temurin_versions() -> List[int]:
    payload = _open_json(f"{TEMURIN_API}/info/available_releases")
    versions = []
    for item in payload.get("available_releases") or []:
        try:
            versions.append(int(item))
        except (TypeError, ValueError):
            continue
    if not versions:
        raise DownloadError("接口未返回任何可用版本。")
    return sorted(versions, reverse=True)


def _azul_versions() -> List[int]:
    """Azul 元数据接口：一次拉取所有 Windows x64 zip JDK，取主版本集合。"""
    url = (
        "https://api.azul.com/metadata/v1/zulu/packages/"
        "?os=windows&arch=x64&archive_type=zip&java_package_type=jdk"
        "&javafx_bundled=false&latest=true&availability_type=C"
    )
    items = _open_json(url)
    majors = set()
    for item in items:
        version = item.get("java_version")
        if version and isinstance(version, (list, tuple)) and version:
            try:
                majors.add(int(version[0]))
            except (TypeError, ValueError):
                continue
    if not majors:
        raise DownloadError("Azul 接口未返回任何可用版本。")
    return sorted(majors, reverse=True)


def _liberica_versions() -> List[int]:
    """BellSoft 接口：一次拉取所有 Windows x64 zip JDK，取 featureVersion 集合。"""
    url = (
        "https://api.bell-sw.com/v1/liberica/releases"
        "?bundle-type=jdk&bitness=64&os=windows&package-type=zip"
    )
    items = _open_json(url, insecure=True)
    majors = set()
    for item in items:
        feature = item.get("featureVersion")
        if isinstance(feature, int):
            majors.add(feature)
    if not majors:
        raise DownloadError("BellSoft 接口未返回任何可用版本。")
    return sorted(majors, reverse=True)


def _probe_versions(build_url: Callable[[int], str]) -> List[int]:
    """对候选主版本号逐个 HEAD 探测，保留真实存在的（用于 Corretto / Microsoft）。"""
    found = []
    for major in _candidate_majors():
        if _is_available(build_url(major)):
            found.append(major)
    if not found:
        raise DownloadError("未能探测到任何可用版本，请检查网络。")
    return sorted(found, reverse=True)


def _is_available(url: str) -> bool:
    try:
        with _open(url, timeout=15, method="HEAD") as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            # 部分站点（如 aka.ms）对无效版本返回 200 的 HTML 页面，需排除
            if "html" in content_type:
                return False
            return response.status == 200
    except DownloadError:
        return False


# --------------------------------------------------------------------------- #
# 下载地址构造
# --------------------------------------------------------------------------- #

def download_url(
    feature_version: int,
    arch: str = "x64",
    image_type: str = "jdk",
    source_id: str = DEFAULT_SOURCE_ID,
) -> str:
    """构造指定厂商免安装 zip 的官方下载地址。"""
    feature_version = int(feature_version)
    image = image_type.lower()

    if source_id == SOURCE_TEMURIN:
        return (
            f"{TEMURIN_API}/binary/latest/{feature_version}/ga/windows/{arch}/"
            f"{image}/hotspot/normal/eclipse"
        )
    if source_id == SOURCE_CORRETTO:
        return _corretto_probe_url(feature_version, arch, image)
    if source_id == SOURCE_MICROSOFT:
        if image != "jdk":
            raise DownloadError("Microsoft Build of OpenJDK 只提供 JDK。")
        return _microsoft_probe_url(feature_version, arch)
    if source_id == SOURCE_AZUL:
        return _azul_download_url(feature_version, arch, image)
    if source_id == SOURCE_LIBERICA:
        return _liberica_download_url(feature_version, arch, image)
    raise DownloadError(f"未知的下载源：{source_id}")


def _corretto_probe_url(version: int, arch: str = "x64", image: str = "jdk") -> str:
    return (
        f"https://corretto.aws/downloads/latest/"
        f"amazon-corretto-{version}-{arch}-windows-{image}.zip"
    )


def _microsoft_probe_url(version: int, arch: str = "x64") -> str:
    return f"https://aka.ms/download-jdk/microsoft-jdk-{version}-windows-{arch}.zip"


def _azul_download_url(version: int, arch: str, image: str) -> str:
    url = (
        "https://api.azul.com/metadata/v1/zulu/packages/"
        f"?java_version={version}&os=windows&arch={arch}&archive_type=zip"
        f"&java_package_type={image}&javafx_bundled=false&crac_supported=false"
        "&latest=true&availability_type=C&result_limit=10"
    )
    items = _open_json(url)
    marker = f"-ca-{image}"
    for item in items:
        name = item.get("name") or ""
        if marker in name and "crac" not in name and "-fx-" not in name:
            link = item.get("download_url")
            if link:
                return str(link)
    if items and items[0].get("download_url"):
        return str(items[0]["download_url"])
    raise DownloadError(f"Azul 未返回 JDK {version} 的下载地址。")


def _liberica_download_url(version: int, arch: str, image: str) -> str:
    bitness = "32" if arch == "x86" else "64"
    url = (
        "https://api.bell-sw.com/v1/liberica/releases"
        f"?version-feature={version}&bundle-type={image}&bitness={bitness}"
        "&os=windows&package-type=zip"
    )
    items = _open_json(url, insecure=True)
    if arch == "aarch64":
        items = [it for it in items if it.get("architecture") == "aarch64"] or items
    for item in reversed(items):
        link = item.get("downloadUrl")
        if link:
            return str(link)
    raise DownloadError(f"BellSoft 未返回 JDK {version} 的下载地址。")


# --------------------------------------------------------------------------- #
# 下载 + 解压
# --------------------------------------------------------------------------- #

def download_and_extract(
    url: str,
    install_root: str,
    on_progress: Optional[ProgressCallback] = None,
    on_status: Optional[StatusCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """下载 zip 并解压到 ``install_root``，返回解压出的 JDK 主目录。"""
    os.makedirs(install_root, exist_ok=True)

    handle, archive = tempfile.mkstemp(suffix=".zip", prefix="jdkdl-")
    os.close(handle)
    try:
        if on_status:
            on_status("正在下载 ...")
        _download(url, archive, on_progress, cancel)

        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("已取消")

        target = os.path.join(install_root, _archive_root(archive))
        if os.path.exists(target):
            raise AlreadyInstalledError(target)

        if on_status:
            on_status("正在解压 ...")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(install_root)
        return target
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass


def _download(
    url: str,
    target: str,
    on_progress: Optional[ProgressCallback],
    cancel: Optional[threading.Event],
) -> None:
    response = _open(url)
    with response, open(target, "wb") as handle:
        head = response.read(4)
        if head[:2] != b"PK":
            raise DownloadError("下载内容不是有效的 zip 压缩包，可能厂商地址已变更。")
        total = int(response.headers.get("Content-Length") or 0)
        done = len(head)
        handle.write(head)
        if on_progress:
            on_progress(done, total)
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("已取消")
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)


def _archive_root(archive: str) -> str:
    """取压缩包内唯一的顶层目录名，例如 ``jdk-17.0.20+8``。"""
    with zipfile.ZipFile(archive) as zf:
        tops = {name.split("/", 1)[0] for name in zf.namelist() if name.split("/", 1)[0]}
    if len(tops) != 1:
        raise DownloadError(f"压缩包结构异常，顶层目录不唯一：{sorted(tops)}")
    return tops.pop()
