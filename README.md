# JDK Manager

Windows 平台的 JDK 版本管理器，图形化界面，支持多版本 JDK 的检测、下载、切换与管理。

> ⚠️ **本项目代码由 AI 辅助生成**，经人工审阅后发布，仅供参考。

## 功能特性

- **本机 JDK 扫描**：自动探测磁盘上已安装的 JDK / JRE，解析版本号、厂商（Temurin、Corretto、Zulu、Microsoft 等）与架构信息
- **在线下载安装**：从各厂商官方渠道直接下载指定版本的 OpenJDK，带下载进度显示
- **一键切换版本**：图形化切换当前生效的 JDK，自动更新 `JAVA_HOME` 与 `PATH` 环境变量
- **删除管理**：支持仅从列表移除，或连同安装文件与环境变量一并删除
- **默认厂商设置**：可配置下载 OpenJDK 时优先使用的官方厂商
- **高 DPI 适配**：界面自动缩放，适配高分辨率屏幕

## 环境要求

- Windows 10 / 11
- Python 3.8 及以上（需包含 tkinter，官方安装包默认自带）

## 快速开始

### 方式一：脚本启动

双击运行 `run.bat`（无控制台窗口）。

### 方式二：命令行启动

```powershell
python main.py
```

### 方式三：打包为 exe

```powershell
pip install pyinstaller
pyinstaller JdkManager.spec
```

打包产物位于 `dist/` 目录。图标可通过 `python make_icon.py` 重新生成。

## 项目结构

```
jdk_manage/
├── main.py                  # 启动入口
├── run.bat                  # 双击启动脚本
├── make_icon.py             # 生成应用图标
├── app.ico                  # 应用图标
├── JdkManager.spec          # PyInstaller 打包配置
└── jdk_manager/
    ├── __init__.py
    ├── ui.py                # Tkinter 主界面与对话框
    ├── jdk_detector.py      # JDK 目录探测与版本解析
    ├── downloader.py        # OpenJDK 下载与解压安装
    ├── env_manager.py       # JAVA_HOME / PATH 环境变量管理
    └── store.py             # JDK 列表与配置的持久化存储
```

## 注意事项

- 修改环境变量仅对**新开的终端**生效
- 本工具仅管理用户级环境变量，不涉及系统级（需管理员）配置
- 下载 JDK 依赖对应厂商官方站点的网络可达性

## License

仅供学习交流使用。
