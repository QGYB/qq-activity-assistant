@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 当前建议使用源码启动。旧版 EXE 曾被 Windows Defender 隔离，原因未确认。
if /i not "%~1"=="--experimental-exe" (
  echo 若需在本机自行测试构建，请运行：打包.cmd --experimental-exe
  exit /b 1
)
if not exist "native\qq_activity_boot.dll" (
  echo 请先按 README 构建 native\qq_activity_boot.dll。
  exit /b 1
)
uv sync
if errorlevel 1 exit /b 1
uv run pyinstaller --noconfirm --clean 群活动助手.spec
if errorlevel 1 exit /b 1
pause
