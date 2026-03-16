#!/bin/bash
# Browser Relay Connect - WSL wrapper
# 用法: bash connect.sh [--confidence 0.7] [--no-open]
#
# 依赖: Windows Python 3 + pyautogui + opencv-python + pillow
# 安装: powershell.exe -Command "pip install pyautogui opencv-python pillow"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Windows 端临时工作目录
WIN_TEMP="/mnt/c/Users/Administrator/Downloads/relay-connect"
mkdir -p "$WIN_TEMP"

# 同步必要文件到 Windows 可访问路径
cp "$SCRIPT_DIR/connect.py" "$WIN_TEMP/connect.py"
cp "$SKILL_DIR/templates/relay_icon.png" "$WIN_TEMP/relay_icon.png" 2>/dev/null
cp "$SKILL_DIR/config.json" "$WIN_TEMP/config.json" 2>/dev/null

# 转换路径
WIN_PY_PATH='C:\Users\Administrator\Downloads\relay-connect\connect.py'
WIN_TPL_PATH='C:\Users\Administrator\Downloads\relay-connect\relay_icon.png'

echo "[connect.sh] Running on Windows Python..."
$PS -NoProfile -Command "python '${WIN_PY_PATH}' --template '${WIN_TPL_PATH}' $*" 2>&1
EXIT_CODE=$?

# 同步结果回来
cp "$WIN_TEMP/config.json" "$SKILL_DIR/config.json" 2>/dev/null
# 同步验证截图
cp "$WIN_TEMP"/_verify_*.png "$SKILL_DIR/templates/" 2>/dev/null
cp "$WIN_TEMP"/_fullscreen_*.png "$SKILL_DIR/templates/" 2>/dev/null

exit $EXIT_CODE
