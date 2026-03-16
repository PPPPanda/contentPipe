# Browser Relay Activator

自动激活 Chrome 中的 OpenClaw Browser Relay 扩展。

## 依赖

- **Windows Python 3** + `pyautogui` + `opencv-python` + `pillow`（通过 desktop-control skill 安装）
- Chrome 已安装 OpenClaw Browser Relay 扩展且已钉住到工具栏
- WSL interop 正常（`powershell.exe` 可调用）

安装依赖：
```bash
powershell.exe -Command "pip install pyautogui opencv-python pillow"
```

## 完整流程

```
┌──────────────────────────────────────────────────┐
│  Step 1: 检测 + 聚焦 Chrome                      │
│  ctypes.EnumWindows 快速检测 chrome.exe 可见窗口   │
│  ├─ 有 → PowerShell ShowWindow(3) + 前台聚焦      │
│  └─ 无 → cmd.exe start chrome → 等 2.5s → 聚焦   │
├──────────────────────────────────────────────────┤
│  Step 2: 模板匹配                                 │
│  pyautogui.screenshot(屏幕顶部 100px)             │
│  cv2.matchTemplate(relay_icon.png) >= 0.7         │
│  ├─ 命中 → 进入 Step 3                           │
│  └─ 未中 → Fallback: 移鼠标到 (0,0) → 重试       │
│            ├─ 命中 → 进入 Step 3                  │
│            └─ 仍未中 → 截工具栏 → VISION_FALLBACK  │
│                        exit 2，调用方 Vision 识别   │
├──────────────────────────────────────────────────┤
│  Step 3: 点击图标                                 │
│  pyautogui.click(center_x, center_y)              │
│  等待 1.5s + 截图验证                              │
├──────────────────────────────────────────────────┤
│  Step 4: （无独立步骤，与 Step 5 合并）             │
├──────────────────────────────────────────────────┤
│  Step 5: 验证连接                                 │
│  截取图标区域 → 检查 badge 颜色                    │
│  红色/绿色像素占比 > 5% → ON → 连接成功            │
│  ├─ 成功 → 输出 {"connected": true} exit 0        │
│  └─ 失败 → 再点击一次 → 再验证                     │
│            ├─ 成功 → exit 0                       │
│            └─ 仍失败 → {"connected": false} exit 0 │
│              + warning: 可能需要手动 attach         │
└──────────────────────────────────────────────────┘
```

### 重要：Toggle 行为

Relay 扩展图标是 **toggle** 按钮（点击 attach / 再点击 detach）。
- 如果已经 ON → 点击会 detach
- 脚本的 Step 5 通过 badge 颜色检测来确认点击后的状态
- 如果点击后发现 OFF（被 detach 了），会再点一次恢复 ON

### Vision Fallback 流程（exit 2 时触发）

当模板匹配两次都失败时（图标外观变化、分辨率不同等），脚本输出 `VISION_FALLBACK`
和工具栏截图路径。调用方应：

1. 用 `image` 工具分析截图，找 relay 图标的屏幕坐标
2. 用 `pyautogui.click(x, y)` 点击识别到的位置
3. 用 `browser action=tabs profile=chrome` 验证连接

## 用法

### 从 WSL 调用（推荐）
```bash
cd ~/work/openclawWS/clawdbot_workspace/skills/browser-relay-activator
bash scripts/connect.sh
```

### 直接 Python 调用（Windows 侧）
```bash
python scripts/connect.py [--confidence 0.7] [--no-open]
```

### 从 OpenClaw exec 调用
```python
exec(
  command="cd ~/work/openclawWS/clawdbot_workspace/skills/browser-relay-activator && bash scripts/connect.sh",
  timeout=30
)
```

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--template` | `templates/relay_icon.png` | 模板图片路径 |
| `--confidence` | `0.7` | 匹配阈值 (0-1) |
| `--no-open` | false | 不打开新 Chrome 窗口 |

## 输出

脚本输出 JSON 到 stdout：

### 成功
```json
{
  "success": true,
  "attempt": 1,
  "method": "template",
  "position": {"x": 2395, "y": 62},
  "confidence": 0.85,
  "verify": "templates/_verify_after_click.png"
}
```

### 失败（需 Vision）
```json
{
  "success": false,
  "error": "VISION_FALLBACK",
  "screenshot": "templates/_fullscreen_fallback.png",
  "message": "..."
}
```

## 验证连接

脚本成功后，调用 `browser(action=tabs, profile=chrome)` 确认 tab 已 attached。

## 模板文件

- `templates/relay_icon.png` — relay 扩展图标模板（小图，约 20x20 ~ 30x30）
- 如果图标外观变了，截取新图标替换此文件

## config.json

记录上次成功的位置和时间，供参考：
```json
{
  "iconPosition": {"x": 2395, "y": 62},
  "lastActivated": "2026-03-16T01:00:00",
  "lastConfidence": 0.85
}
```

## 关键经验

1. **Chrome 必须在前台** — 模板匹配只对屏幕可见像素生效
2. **鼠标不能遮挡图标** — 如果鼠标悬停在扩展图标上会改变外观，导致匹配失败
3. **只截工具栏区域** — 加速匹配 + 减少误匹配
4. **点击后不跑多余操作** — 只截图验证，不做 OCR 等
5. **Vision 是最后手段** — 模板匹配 > 移鼠标重试 > Vision 识别
