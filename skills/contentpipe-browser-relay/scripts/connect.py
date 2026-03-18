#!/usr/bin/env python3
"""
Browser Relay Connect — pyautogui + opencv 版

流程：
1. 打开新 Chrome 窗口 + 最大化 + 聚焦
2. 模板匹配找 relay 图标 (confidence >= 0.7)
3. 匹配成功 → 点击图标
4. 匹配失败 → 移鼠标到 (0,0) 避遮挡 → 重试
5. 仍失败 → 截全屏保存，输出 VISION_FALLBACK 让调用方用 Vision 识别
6. 截图验证点击结果

用法: python connect.py [--template path] [--confidence 0.7]
"""

import sys
import os
import time
import json
import argparse

try:
    import pyautogui
    import cv2
except ImportError as e:
    print(json.dumps({"success": False, "error": f"missing_dep: {e}"}))
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
# 输出目录：优先用脚本所在目录（Windows 端可写），fallback 到 templates
OUTPUT_DIR = SCRIPT_DIR
TEMPLATES_DIR = os.path.join(SKILL_DIR, "templates")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")  # 存到脚本同级
DEFAULT_TEMPLATE = os.path.join(TEMPLATES_DIR, "relay_icon.png")

# pyautogui 安全设置
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


def log(msg):
    print(f"[connect] {msg}", flush=True)


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def check_chrome_visible():
    """检查是否有 Chrome 窗口可见（有主窗口句柄 = 有可见窗口）。
    返回 True 表示 Chrome 已经有窗口打开，不需要 start chrome。
    注意：不检查前台，因为 Python 自身会抢前台。"""
    try:
        import ctypes
        from ctypes import wintypes

        # EnumWindows 回调: 找所有 chrome.exe 的可见窗口
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = [False]

        def callback(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                # 获取进程名
                h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid.value)  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
                if h:
                    buf = ctypes.create_unicode_buffer(260)
                    size = wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        if "chrome.exe" in buf.value.lower():
                            found[0] = True
                    kernel32.CloseHandle(h)
            return not found[0]  # stop enumeration if found

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return found[0]
    except Exception:
        return False


def focus_chrome():
    """聚焦 Chrome 窗口并最大化。如果已有可见 Chrome 窗口则直接聚焦，否则打开新窗口。"""
    log("Step 1: Focus + maximize Chrome...")

    # 快速检查：是否有可见的 Chrome 窗口
    chrome_exists = check_chrome_visible()
    if chrome_exists:
        log("  -> Chrome window detected, bringing to front...")
    else:
        log("  -> No Chrome window found")
        return False

    ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    ps_script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}}
"@
$p = Get-Process chrome -EA SilentlyContinue | Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} | Sort-Object StartTime -Descending | Select-Object -First 1
if ($p) {{
    [W]::ShowWindow($p.MainWindowHandle, 3) | Out-Null
    Start-Sleep -Milliseconds 300
    [W]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
    Write-Output "OK:$($p.MainWindowTitle)"
}} else {{
    Write-Output "FAIL:no_chrome"
}}
"""
    ps_path = os.path.join(OUTPUT_DIR, "_focus.ps1")
    with open(ps_path, "w") as f:
        f.write(ps_script.strip())

    import subprocess
    result = subprocess.run(
        [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
        capture_output=True, text=True, timeout=10
    )
    output = result.stdout.strip()
    log(f"  -> {output}")
    return output.startswith("OK:")


def open_chrome():
    """打开新 Chrome 窗口"""
    log("Opening new Chrome window...")
    import subprocess
    try:
        subprocess.Popen(
            ["cmd.exe", "/C", "start", "chrome", "--new-window", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        subprocess.Popen(
            ["cmd.exe", "/C", "start", "", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             "--new-window", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    time.sleep(2.5)


def template_match(template_path, confidence=0.7):
    """
    模板匹配：在屏幕上查找 template 图片
    返回 (x, y, match_val) 或 None
    """
    log(f"Template matching (confidence >= {confidence})...")

    if not os.path.exists(template_path):
        log(f"  FAIL Template not found: {template_path}")
        return None

    # 截取工具栏区域（屏幕顶部 100px）加速匹配
    screen_w, screen_h = pyautogui.size()
    toolbar_region = (0, 0, screen_w, 100)
    screen_img = pyautogui.screenshot(region=toolbar_region)

    import numpy as np
    screen_np = np.array(screen_img)
    screen_bgr = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)

    template = cv2.imread(template_path)
    if template is None:
        log(f"  FAIL Cannot read template: {template_path}")
        return None

    th, tw = template.shape[:2]
    result = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    log(f"  Best match: {max_val:.3f} at ({max_loc[0]}, {max_loc[1]})")

    if max_val >= confidence:
        # 返回匹配中心点的绝对屏幕坐标
        cx = max_loc[0] + tw // 2
        cy = max_loc[1] + th // 2  # toolbar_region starts at y=0, so no offset needed
        log(f"  OK Match found: center=({cx}, {cy}), confidence={max_val:.3f}")
        return (cx, cy, max_val)

    log(f"  FAIL No match above threshold (best: {max_val:.3f})")
    return None


def check_relay_connected(icon_x, icon_y):
    """检查 relay 是否已连接：截取图标区域，检查是否出现 'ON' badge 的颜色特征。
    
    Relay 扩展连接后图标上会显示绿色 "ON" badge。
    通过检查图标区域是否有足够的绿色像素来判断。
    """
    import numpy as np

    # 截取图标周围小区域
    region = (max(0, icon_x - 15), max(0, icon_y - 15), 30, 30)
    img = pyautogui.screenshot(region=region)
    img_np = np.array(img)

    # 检查绿色像素数量（ON badge 是绿色的）
    # 绿色范围: R<150, G>100, B<150
    green_mask = (img_np[:, :, 0] < 150) & (img_np[:, :, 1] > 100) & (img_np[:, :, 2] < 150)
    green_pixels = np.sum(green_mask)
    total_pixels = img_np.shape[0] * img_np.shape[1]
    green_ratio = green_pixels / total_pixels

    log(f"  Icon green ratio: {green_ratio:.3f} ({green_pixels}/{total_pixels} pixels)")

    # 如果绿色占比超过 5%，认为是 ON 状态
    if green_ratio > 0.05:
        log("  -> Relay badge shows ON (green detected)")
        return True

    # 也检查是否有红/粉色 badge（某些版本用红色表示 ON）
    red_mask = (img_np[:, :, 0] > 150) & (img_np[:, :, 1] < 100) & (img_np[:, :, 2] < 100)
    red_pixels = np.sum(red_mask)
    red_ratio = red_pixels / total_pixels
    log(f"  Icon red ratio: {red_ratio:.3f}")

    if red_ratio > 0.05:
        log("  -> Relay badge shows ON (red detected)")
        return True

    log("  -> No ON badge detected")
    return False


def click_and_verify(x, y, label="click"):
    """点击坐标并截图验证"""
    log(f"Clicking at ({x}, {y})...")
    pyautogui.click(x, y)
    time.sleep(1.5)

    # 截图验证
    vx = max(0, x - 150)
    vy = max(0, y - 100)
    verify_img = pyautogui.screenshot(region=(vx, vy, 300, 200))
    verify_path = os.path.join(OUTPUT_DIR, f"_verify_{label}.png")
    verify_img.save(verify_path)
    log(f"  Verify screenshot: _verify_{label}.png")
    return verify_path


def fullscreen_screenshot():
    """截全屏保存供 Vision fallback 诊断"""
    path = os.path.join(OUTPUT_DIR, "_fullscreen_fallback.png")
    # 只截工具栏区域节省文件大小
    screen_w, _ = pyautogui.size()
    img = pyautogui.screenshot(region=(screen_w // 2, 0, screen_w // 2, 100))
    img.save(path)
    log(f"  Toolbar screenshot saved: _fullscreen_fallback.png")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, help="Template image path")
    parser.add_argument("--confidence", type=float, default=0.7, help="Match threshold")
    parser.add_argument("--no-open", action="store_true", help="Don't open new Chrome window")
    args = parser.parse_args()

    cfg = load_config()

    # Step 1: Chrome 聚焦（如果没有 Chrome 则打开新窗口）
    if not args.no_open:
        if not focus_chrome():
            open_chrome()
            if not focus_chrome():
                print(json.dumps({"success": False, "error": "chrome_not_found"}))
                sys.exit(1)
    else:
        focus_chrome()

    time.sleep(0.5)

    # Step 2: 模板匹配（第一次）
    match = template_match(args.template, args.confidence)

    if not match:
        # Step 3: Fallback — 移鼠标到 (0,0) 避免遮挡扩展图标，重试
        log("Fallback: moving mouse to (0, 0)...")
        pyautogui.moveTo(0, 0)
        time.sleep(0.5)
        match = template_match(args.template, args.confidence)

    if not match:
        # Step 4: 模板匹配彻底失败 → 截图给 Vision
        log("Template match failed twice. Saving screenshot for Vision fallback...")
        screenshot_path = fullscreen_screenshot()
        print(json.dumps({
            "success": False, "error": "VISION_FALLBACK",
            "screenshot": screenshot_path,
            "message": "Template match failed. Use Vision to identify relay icon position in the screenshot."
        }))
        sys.exit(2)

    # 模板匹配成功，执行点击
    x, y, conf = match

    # Step 3/4: 点击 + Step 5: 验证连接（最多重试一次）
    for attempt in range(1, 3):
        label = "after_click" if attempt == 1 else "retry_click"
        verify = click_and_verify(x, y, label)

        # Step 5: 检查 relay 是否真的连上了（通过图标 badge 颜色）
        log(f"Step 5: Checking relay connection (attempt {attempt})...")
        time.sleep(1)  # 额外等待 relay handshake
        connected = check_relay_connected(x, y)

        if connected:
            cfg["iconPosition"] = {"x": x, "y": y}
            cfg["lastActivated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            cfg["lastConfidence"] = round(conf, 3)
            save_config(cfg)
            print(json.dumps({
                "success": True, "attempt": attempt, "method": "template",
                "position": {"x": x, "y": y}, "confidence": round(conf, 3),
                "connected": True, "verify": verify
            }))
            sys.exit(0)

        if attempt == 1:
            log("  Relay not connected after click. Retrying click...")
            time.sleep(1)

    # 两次点击都没连上
    cfg["iconPosition"] = {"x": x, "y": y}
    cfg["lastActivated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    cfg["lastConfidence"] = round(conf, 3)
    save_config(cfg)
    print(json.dumps({
        "success": True, "attempt": 2, "method": "template",
        "position": {"x": x, "y": y}, "confidence": round(conf, 3),
        "connected": False, "verify": verify,
        "warning": "Clicked twice but relay connection not confirmed. May need manual attach."
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
