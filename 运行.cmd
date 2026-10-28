@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "启动桌面.pyw"
  exit /b
)
where uv >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python 环境。请安装 uv 后重新运行，或检查桌面快捷方式。
  pause
  exit /b 1
)
uv run python "启动桌面.pyw"
