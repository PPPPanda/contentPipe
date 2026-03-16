#!/bin/bash
# Browser Relay Activator - WSL wrapper
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WIN_BASE="E:\\clawdbot_bridge\\clawdbot_workspace\\skills\\browser-relay-activator"

$PS -Command "cd '$WIN_BASE'; node scripts/activate.js $*" 2>&1
