"""配置持久化：配置写入宿主文件（exe 或 main.py）的 NTFS 备用数据流（ADS），
随文件本身携带，不产生独立配置文件，也不依赖注册表。

数据流形式：<宿主文件>:jdkmgr.config
非 NTFS 卷（U 盘/网络共享等）不支持 ADS 时，自动回退到注册表，保证功能可用。
"""

from __future__ import annotations

import json
import os
import sys
import winreg
from typing import Any, Dict, List, Optional

APP_NAME = "JdkManager"
STREAM_NAME = "jdkmgr.config"
REG_KEY_PATH = f"Software\\{APP_NAME}"
REG_VALUE_NAME = "config"


def _validate(data: Any) -> Dict[str, Any]:
    """规范化配置结构，保证 jdks 为字符串列表。"""
    if not isinstance(data, dict):
        return {"jdks": []}
    homes = data.get("jdks")
    if not isinstance(homes, list):
        homes = []
    data["jdks"] = [item for item in homes if isinstance(item, str)]
    return data


def host_file() -> str:
    """承载配置的宿主文件：打包后是 exe 自身，源码运行时是入口脚本。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


def stream_path() -> str:
    return f"{host_file()}:{STREAM_NAME}"


# ---------- ADS 读写 ----------


def _read_stream() -> Optional[Dict[str, Any]]:
    try:
        with open(stream_path(), "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        return _validate(json.loads(raw))
    except (ValueError, TypeError):
        return None


def _write_stream(data: Dict[str, Any]) -> bool:
    try:
        with open(stream_path(), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False))
        return True
    except OSError:
        return False


# ---------- 注册表回退 ----------


def _read_registry() -> Optional[Dict[str, Any]]:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ
        ) as key:
            raw, _ = winreg.QueryValueEx(key, REG_VALUE_NAME)
    except OSError:
        return None
    try:
        return _validate(json.loads(raw))
    except (ValueError, TypeError):
        return None


def _write_registry(data: Dict[str, Any]) -> None:
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH) as key:
            winreg.SetValueEx(
                key,
                REG_VALUE_NAME,
                0,
                winreg.REG_SZ,
                json.dumps(data, ensure_ascii=False),
            )
    except OSError:
        pass


def _delete_registry() -> None:
    """配置已落到 ADS 后，清理注册表回退值。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, REG_VALUE_NAME)
    except OSError:
        pass


# ---------- 旧版本迁移 ----------


def _legacy_config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "config.json")


def _migrate_legacy() -> Optional[Dict[str, Any]]:
    path = _legacy_config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return _validate(data)


# ---------- 对外接口 ----------


def load_config() -> Dict[str, Any]:
    data = _read_stream()
    if data is None:
        data = _read_registry()
    if data is None:
        data = _migrate_legacy()
    if data is None:
        return {"jdks": []}
    # 把迁移/回退来的配置固化到 ADS；成功后清理注册表
    if _write_stream(data):
        _delete_registry()
    elif _read_registry() is None:
        _write_registry(data)
    return data


def save_config(data: Dict[str, Any]) -> None:
    if not _write_stream(data):
        _write_registry(data)


def load_jdk_homes() -> List[str]:
    return list(load_config().get("jdks", []))


def save_jdk_homes(homes: List[str]) -> None:
    data = load_config()
    data["jdks"] = list(homes)
    save_config(data)


def load_settings() -> Dict[str, Any]:
    """读取下载相关设置，返回原始字典（可能为空，由调用方套用默认值）。"""
    settings = load_config().get("settings")
    return dict(settings) if isinstance(settings, dict) else {}


def save_settings(settings: Dict[str, Any]) -> None:
    data = load_config()
    current = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    current.update(settings)
    data["settings"] = current
    save_config(data)
