#!/usr/bin/env node
/**
 * Browser Relay Activator v2
 * 
 * 核心改进: 使用实际 Chrome 窗口位置而非假设全屏
 * 
 * 流程:
 *   1. 弹出新 Chrome 窗口 (或聚焦已有)
 *   2. 获取 Chrome 窗口的实际位置和尺寸
 *   3. 定位扩展图标 (基于窗口坐标)
 *   4. 点击激活
 *   5. 如扩展异常 → 修复后重试
 * 
 * 用法:
 *   node activate.js                    # 自动模式
 *   node activate.js --x 1200 --y 150   # 指定坐标
 *   node activate.js --pos 2            # 使用第 N 个候选位置
 *   node activate.js --launch           # 强制启动新 Chrome 窗口
 *   node activate.js --scan             # 仅截图扫描，不点击
 *   node activate.js --repair           # 直接进入修复模式
 */

const path = require('path');
const fs = require('fs');
const { execSync } = require('child_process');

const WC_PATH = path.join(__dirname, '..', '..', 'windows-control', 'scripts', 'win-control');
const WindowsControl = require(WC_PATH);

const TEMPLATES_DIR = path.join(__dirname, '..', 'templates');
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

if (!fs.existsSync(TEMPLATES_DIR)) fs.mkdirSync(TEMPLATES_DIR, { recursive: true });

const sleep = ms => new Promise(r => setTimeout(r, ms));

function log(msg) { console.error(`[relay] ${msg}`); }
function output(result) { console.log(JSON.stringify(result, null, 2)); }

function loadConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')); } catch { return {}; }
}
function saveConfig(cfg) { fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2)); }

// ============================================================
// 获取 Chrome 窗口位置 (通过 PowerShell + Win32 API)
// ============================================================
const PS_EXE = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';

function getChromeWindowRect() {
  // 写 PS 脚本到临时文件避免引号转义问题
  const psScript = path.join(TEMPLATES_DIR, '_get_chrome.ps1');
  fs.writeFileSync(psScript, `
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
$procs = Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero }
foreach ($p in $procs) {
    $r = New-Object WinAPI+RECT
    [WinAPI]::GetWindowRect($p.MainWindowHandle, [ref]$r) | Out-Null
    Write-Output "$($r.Left),$($r.Top),$($r.Right),$($r.Bottom),$($p.MainWindowHandle),$($p.MainWindowTitle)"
}
  `.trim());
  
  try {
    const raw = execSync(
      `"${PS_EXE}" -NoProfile -ExecutionPolicy Bypass -File "${psScript}"`,
      { encoding: 'utf8', timeout: 15000 }
    ).trim();
    
    if (!raw) return null;
    
    const windows = raw.split('\n').map(line => {
      const parts = line.trim().split(',');
      const l = parseInt(parts[0]), t = parseInt(parts[1]);
      const r = parseInt(parts[2]), b = parseInt(parts[3]);
      const hwnd = parts[4] || '0';
      const title = parts.slice(5).join(',');
      return {
        left: l, top: t, right: r, bottom: b,
        width: r - l, height: b - t, hwnd, title
      };
    }).filter(w => w.width > 100 && w.height > 100);
    
    if (windows.length === 0) return null;
    
    const blank = windows.find(w => w.title.includes('about:blank'));
    return blank || windows.sort((a, b) => (b.width * b.height) - (a.width * a.height))[0];
  } catch (e) {
    log(`GetWindowRect failed: ${e.message}`);
    return null;
  }
}

// ============================================================
// Focus Chrome window (bring to foreground before clicking)
// ============================================================
function focusChromeWindow(chromeWin) {
  if (!chromeWin || !chromeWin.hwnd || chromeWin.hwnd === '0') {
    log('No window handle available, cannot focus');
    return false;
  }
  const psScript = path.join(TEMPLATES_DIR, '_focus_chrome.ps1');
  fs.writeFileSync(psScript, `
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FocusAPI {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
$hwnd = [IntPtr]::new(${chromeWin.hwnd})
[FocusAPI]::ShowWindow($hwnd, 9) | Out-Null
[FocusAPI]::SetForegroundWindow($hwnd) | Out-Null
Write-Output "focused"
  `.trim());
  try {
    const result = execSync(
      `"${PS_EXE}" -NoProfile -ExecutionPolicy Bypass -File "${psScript}"`,
      { encoding: 'utf8', timeout: 5000 }
    ).trim();
    log(`Focus Chrome: ${result}`);
    return result.includes('focused');
  } catch (e) {
    log(`Focus Chrome failed: ${e.message}`);
    return false;
  }
}

// ============================================================
// Step 1: 弹出新 Chrome 窗口
// ============================================================
async function launchChrome(ctrl, forceNew = false) {
  log('Step 1: Launching Chrome...');
  
  // 检查是否已有 Chrome 窗口
  if (!forceNew) {
    const existing = getChromeWindowRect();
    if (existing) {
      log(`Found existing Chrome: ${existing.left},${existing.top} ${existing.width}x${existing.height} "${existing.title}"`);
      return existing;
    }
  }
  
  try {
    execSync('cmd.exe /C start chrome --new-window about:blank', {
      stdio: 'ignore', timeout: 5000
    });
  } catch {
    try {
      execSync('cmd.exe /C start "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --new-window about:blank', {
        stdio: 'ignore', timeout: 5000
      });
    } catch { /* ignore */ }
  }
  
  log('Waiting for Chrome...');
  await sleep(3000);
  
  const win = getChromeWindowRect();
  if (!win) {
    throw new Error('Chrome window not found after launch');
  }
  
  log(`Chrome window: L=${win.left} T=${win.top} R=${win.right} B=${win.bottom} (${win.width}x${win.height})`);
  return win;
}

// ============================================================
// Step 2: 定位扩展图标
// ============================================================
async function findExtensionIcon(ctrl, chromeWin) {
  log('Step 2: Locating extension icons...');
  
  const config = loadConfig();
  
  // Chrome UI 布局常量 (基于窗口左上角偏移)
  // 标签栏: 窗口顶部 ~ 40px
  // 地址栏: 窗口顶部 + 40 ~ 76px  (中心 y ≈ 55)
  // 三点菜单: 窗口右边 - 20px
  // 扩展图标: 地址栏右侧, 三点菜单左边, 间距 ~30px
  
  const TOOLBAR_CENTER_Y = chromeWin.top + 56;  // 地址栏垂直中心
  const MENU_X = chromeWin.right - 20;           // 三点菜单 X
  
  // 策略 A: 使用已保存的模板图像匹配
  const templatePath = path.join(TEMPLATES_DIR, 'relay_icon.png');
  if (fs.existsSync(templatePath)) {
    log('Trying template match...');
    const result = await ctrl.find(templatePath, { confidence: 0.6 });
    if (result.success && result.data?.found) {
      log(`Template match: (${result.data.center.x}, ${result.data.center.y})`);
      return { position: result.data.center, method: 'template' };
    }
    log('Template match failed');
  }
  
  // 策略 B: 使用保存的坐标 (如果 Chrome 窗口位置没变)
  if (config.iconPosition && config.chromeWindow) {
    const saved = config.chromeWindow;
    if (saved.left === chromeWin.left && saved.top === chromeWin.top &&
        saved.width === chromeWin.width) {
      log(`Using saved position: (${config.iconPosition.x}, ${config.iconPosition.y})`);
      return { position: config.iconPosition, method: 'saved' };
    }
    log('Chrome window moved, saved position invalid');
  }
  
  // 策略 C: 截图工具栏并生成候选位置
  log('Scanning toolbar for extension icons...');
  
  // 截取地址栏右侧区域
  const scanRegion = {
    x: Math.max(0, chromeWin.left + Math.floor(chromeWin.width * 0.6)),
    y: Math.max(0, chromeWin.top + 30),
    width: Math.floor(chromeWin.width * 0.4),
    height: 50
  };
  
  const scanPath = path.join(TEMPLATES_DIR, '_toolbar_scan.png');
  await ctrl.screenshot({ output: scanPath, region: scanRegion });
  log(`Toolbar scan: ${scanPath} (region: ${scanRegion.x},${scanRegion.y} ${scanRegion.width}x${scanRegion.height})`);
  
  // 生成候选位置: 从三点菜单往左, 间距 30px
  const candidates = [];
  for (let i = 1; i <= 10; i++) {
    candidates.push({
      x: MENU_X - (30 * i),
      y: TOOLBAR_CENTER_Y,
      desc: `ext_icon_${i}`
    });
  }
  
  log(`Generated ${candidates.length} candidates: first=(${candidates[0].x}, ${candidates[0].y}) last=(${candidates[candidates.length-1].x}, ${candidates[candidates.length-1].y})`);
  
  return { candidates, method: 'scan', scanRegion };
}

// ============================================================
// Step 3: 点击扩展图标
// ============================================================
async function clickIcon(ctrl, position) {
  log(`Step 3: Clicking at (${position.x}, ${position.y})...`);
  await ctrl.click(position.x, position.y);
  await sleep(1000);
}

// ============================================================
// Step 4: 检测扩展状态
// ============================================================
async function checkStatus(ctrl, chromeWin) {
  log('Step 4: Checking status...');
  await sleep(500);
  
  // 截图记录状态
  const statePath = path.join(TEMPLATES_DIR, '_post_click.png');
  await ctrl.screenshot({ output: statePath });
  
  // OCR 检查是否有错误
  try {
    const ocrRegion = {
      x: chromeWin.left + Math.floor(chromeWin.width * 0.2),
      y: chromeWin.top + 70,
      width: Math.floor(chromeWin.width * 0.6),
      height: 200
    };
    
    const ocrResult = await ctrl.ocrText({ region: ocrRegion });
    if (ocrResult.success) {
      const text = ocrResult.data.text.toLowerCase();
      if (text.includes('error') || text.includes('crash') || text.includes('cannot')) {
        log(`⚠️ Error detected: "${ocrResult.data.text.substring(0, 80)}"`);
        return { ok: false, error: ocrResult.data.text.substring(0, 80) };
      }
      log(`OCR text (first 60): "${ocrResult.data.text.substring(0, 60)}"`);
    }
  } catch (e) {
    log(`OCR skipped: ${e.message}`);
  }
  
  return { ok: true };
}

// ============================================================
// 修复: 重新加载扩展
// ============================================================
async function repairExtension(ctrl) {
  log('=== Repair: Reloading extension ===');
  
  // 打开 chrome://extensions
  await ctrl.hotkey('ctrl+l');
  await sleep(300);
  await ctrl.type('chrome://extensions');
  await ctrl.press('enter');
  await sleep(2500);
  
  // 查找 "Clawdbot" 或 "Browser Relay"
  log('Searching for Clawdbot extension...');
  const findResult = await ctrl.findText('Clawdbot', { lang: 'eng' });
  
  if (!findResult.success || !findResult.data?.found) {
    const findResult2 = await ctrl.findText('Browser Relay', { lang: 'eng' });
    if (!findResult2.success || !findResult2.data?.found) {
      log('Extension not found on page');
      return false;
    }
  }
  
  // 查找 reload 按钮
  const reloadBtn = await ctrl.findText('reload', { lang: 'eng' });
  if (reloadBtn.success && reloadBtn.data?.found) {
    log('Clicking reload...');
    await ctrl.click(reloadBtn.data.center.x, reloadBtn.data.center.y);
    await sleep(2000);
  }
  
  // 回到空白页
  await ctrl.hotkey('ctrl+l');
  await sleep(200);
  await ctrl.type('about:blank');
  await ctrl.press('enter');
  await sleep(1000);
  
  log('=== Repair complete ===');
  return true;
}

// ============================================================
// 主流程
// ============================================================
async function main() {
  const args = parseArgs();
  const ctrl = new WindowsControl({ screen: { confidence: 0.7 } });
  
  try {
    await ctrl.init();
    log('Initialized');
    
    // Step 1: Chrome 窗口
    const chromeWin = await launchChrome(ctrl, !!args.launch);
    
    // Scan 模式: 只截图不点击
    if (args.scan) {
      const scanResult = await findExtensionIcon(ctrl, chromeWin);
      
      // 截取整个 Chrome 顶部
      const topPath = path.join(TEMPLATES_DIR, '_chrome_top_full.png');
      await ctrl.screenshot({
        output: topPath,
        region: {
          x: chromeWin.left,
          y: chromeWin.top,
          width: chromeWin.width,
          height: 120
        }
      });
      
      output({
        success: true,
        mode: 'scan',
        chromeWindow: chromeWin,
        screenshot: topPath,
        candidates: scanResult.candidates || null,
        toolbarScan: scanResult.scanRegion || null
      });
      return;
    }
    
    // Repair 模式
    if (args.repair) {
      const ok = await repairExtension(ctrl);
      output({ success: ok, mode: 'repair' });
      return;
    }
    
    // Step 2: 定位
    const iconResult = await findExtensionIcon(ctrl, chromeWin);
    
    let clickPosition;
    
    if (args.x && args.y) {
      // 指定坐标
      clickPosition = { x: parseInt(args.x), y: parseInt(args.y) };
      log(`Using specified position: (${clickPosition.x}, ${clickPosition.y})`);
    } else if (iconResult.position) {
      // 模板匹配或保存的位置
      clickPosition = iconResult.position;
    } else if (iconResult.candidates) {
      // 候选位置
      const idx = args.pos ? parseInt(args.pos) - 1 : 0;
      clickPosition = iconResult.candidates[Math.min(idx, iconResult.candidates.length - 1)];
      log(`Using candidate #${idx + 1}: (${clickPosition.x}, ${clickPosition.y})`);
    }
    
    if (!clickPosition) {
      throw new Error('Could not determine click position');
    }
    
    // Step 2.5: 聚焦 Chrome 窗口 (防止被其他窗口遮挡)
    focusChromeWindow(chromeWin);
    await sleep(500);
    
    // Step 3: 点击
    await clickIcon(ctrl, clickPosition);
    
    // Step 4: 检查
    const status = await checkStatus(ctrl, chromeWin);
    
    if (!status.ok && !args['no-repair']) {
      log('Attempting repair...');
      const repaired = await repairExtension(ctrl);
      if (repaired) {
        // 重新点击
        await sleep(1000);
        await clickIcon(ctrl, clickPosition);
      }
    }
    
    // 保存配置
    const config = loadConfig();
    config.iconPosition = clickPosition;
    config.chromeWindow = {
      left: chromeWin.left, top: chromeWin.top,
      width: chromeWin.width, height: chromeWin.height
    };
    config.lastActivated = new Date().toISOString();
    saveConfig(config);
    
    // 最终截图
    const finalPath = path.join(TEMPLATES_DIR, '_final_state.png');
    await ctrl.screenshot({ output: finalPath });
    
    output({
      success: true,
      clicked: clickPosition,
      method: iconResult.method,
      chromeWindow: chromeWin,
      status: status,
      finalScreenshot: finalPath
    });
    
  } catch (e) {
    log(`Error: ${e.message}`);
    output({ success: false, error: e.message });
    process.exit(1);
  }
}

function parseArgs() {
  const args = {};
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      args[key] = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true;
      if (args[key] !== true) i++;
    }
  }
  return args;
}

main();
