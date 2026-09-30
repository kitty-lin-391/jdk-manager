"""JDK 版本管理器启动入口。

用法：
    python main.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jdk_manager.ui import main  # noqa: E402

if __name__ == "__main__":
    main()
