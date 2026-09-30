@echo off
rem 双击启动 JDK 版本管理器（不显示控制台窗口）
setlocal

set "DIR=%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"

if exist "%PYW%" (
    start "" "%PYW%" "%DIR%main.py"
    goto :eof
)

rem 回退：使用 PATH 中的 pythonw / python
where pythonw.exe >nul 2>nul && (
    start "" pythonw.exe "%DIR%main.py"
    goto :eof
)

where python.exe >nul 2>nul && (
    python "%DIR%main.py"
    goto :eof
)

echo 未找到 Python，请先安装 Python 3.8 及以上版本。
pause
