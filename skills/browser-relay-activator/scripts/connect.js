#!/usr/bin/env node
/**
 * Browser Relay Connect — 精简版
 * 
 * 经验总结：
 * 1. 必须先打开新 Chrome 窗口并最大化（确保在前台）
 * 2. 模板匹配在 Chrome 可见时才有效
 * 3. 点击只需纯粹点击，不要跑 OCR 等额外操作
 * 4. 如果首次失败：移鼠标到 0,0 → 重新模板匹配 → 截图验证 → 再点击
 * 
 * 用法: node connect.js
 */

const path = require('path');
const fs = require('fs');
const { execSync } = require('child_process');

const SKILL_DIR = path.join(__dirname, '..');
const TEMPLATES_DIR = path.join(SKILL_DIR, 'templates');
const TEMPLATE_ICON = path.join(TEMPLATES_DIR, 'relay_icon.png');
const WC_PATH = path.join(SKILL_DIR, '..', 'windows-control', 'scripts', 'win-control');

function log(msg) {
  console.log(`[connect] ${msg}`);
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ============================================================
// Step 1: 打开新 Chrome 窗口 + 最大化 + 聚焦
// ============================================================
function openAndMaximizeChrome() {
  log('Step 1: Opening new Chrome window + maximize...');
  
  try {
    execSync('cmd.exe /C start chrome --new-window about:blank', {
      timeout: 5000, stdio: 'ignore'
    });
  } catch {
    execSync('cmd.exe /C start "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --new-window about:blank', {
      timeout: 5000, stdio: 'ignore'
    });
  }
  
  // 等待窗口出现 (用 ping 代替 timeout，兼容性更好)
  execSync('ping -n 3 127.0.0.1 > nul', { shell: 'cmd.exe', timeout: 10000 });
  
  // 最大化 + 聚焦
  const PS_EXE = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';
  const psScript = path.join(TEMPLATES_DIR, '_focus.ps1');
  fs.writeFileSync(psScript, `
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
"@
$p = Get-Process chrome -EA SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Sort-Object StartTime -Descending | Select-Object -First 1
if ($p) {
    [W]::ShowWindow($p.MainWindowHandle, 3) | Out-Null
    Start-Sleep -Milliseconds 200
    [W]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
    Write-Output "OK:$($p.MainWindowTitle)"
} else {
    Write-Output "FAIL:no_chrome"
}
  `.trim());

  const result = execSync(
    `"${PS_EXE}" -NoProfile -ExecutionPolicy Bypass -File "${psScript}"`,
    { encoding: 'utf8', timeout: 10000 }
  ).trim();
  
  log(`  → ${result}`);
  return result.startsWith('OK:');
}

// ============================================================
// Step 2: 模板匹配找扩展图标
// ============================================================
async function findIcon(ctrl) {
  log('Step 2: Template matching for relay icon...');
  
  if (!fs.existsSync(TEMPLATE_ICON)) {
    log('  ✗ Template file not found: relay_icon.png');
    return null;
  }
  
  try {
    const result = await ctrl.find(TEMPLATE_ICON, { confidence: 0.6 });
    if (result.success && result.data && result.data.center) {
      const pos = result.data.center;
      log(`  ✓ Found at (${pos.x}, ${pos.y})`);
      return pos;
    }
  } catch (e) {
    log(`  ✗ Match error: ${e.message}`);
  }
  
  log('  ✗ Template match failed');
  return null;
}

// ============================================================
// Step 3: 点击坐标
// ============================================================
async function clickAt(ctrl, x, y) {
  log(`Step 3: Clicking at (${x}, ${y})...`);
  const { mouse, Point } = require(
    path.join(SKILL_DIR, '..', 'windows-control', 'lib', 'nut-simple', 'index.js')
  );
  await mouse.setPosition(new Point(x, y));
  await mouse.leftClick();
  log('  ✓ Clicked');
}

// ============================================================
// Step 4: 截图验证（在点击位置附近截一小块）
// ============================================================
async function screenshotArea(ctrl, centerX, centerY, label) {
  const outPath = path.join(TEMPLATES_DIR, `_verify_${label}.png`);
  const x = Math.max(0, centerX - 150);
  const y = Math.max(0, centerY - 100);
  await ctrl.screenshot({
    output: outPath,
    region: { x, y, width: 300, height: 300 }
  });
  log(`  Screenshot saved: _verify_${label}.png`);
  return outPath;
}

// ============================================================
// Main
// ============================================================
async function main() {
  const WC = require(WC_PATH);
  const ctrl = new WC();
  await ctrl.init();
  
  // Step 1: 打开新 Chrome 并最大化
  const chromeOk = openAndMaximizeChrome();
  if (!chromeOk) {
    console.log(JSON.stringify({ success: false, error: 'chrome_not_found' }));
    process.exit(1);
  }
  await sleep(1000);
  
  // Step 2: 模板匹配
  let iconPos = await findIcon(ctrl);
  
  if (iconPos) {
    // Step 3: 点击
    await clickAt(ctrl, iconPos.x, iconPos.y);
    await sleep(1500);
    
    // Step 4: 截图验证
    await screenshotArea(ctrl, iconPos.x, iconPos.y, 'after_click');
    
    console.log(JSON.stringify({
      success: true,
      attempt: 1,
      position: iconPos,
      method: 'template',
      verify: '_verify_after_click.png'
    }));
    process.exit(0);
  }
  
  // ========== 失败 fallback ==========
  log('First attempt failed. Running fallback...');
  
  // Fallback A: 移动鼠标到 (0, 0) 避免遮挡
  log('Fallback: Moving mouse to (0, 0)...');
  const { mouse, Point } = require(
    path.join(SKILL_DIR, '..', 'windows-control', 'lib', 'nut-simple', 'index.js')
  );
  await mouse.setPosition(new Point(0, 0));
  await sleep(500);
  
  // Fallback B: 重新模板匹配
  log('Fallback: Retrying template match...');
  iconPos = await findIcon(ctrl);
  
  if (!iconPos) {
    // 截全屏看看怎么回事
    const fullPath = path.join(TEMPLATES_DIR, '_verify_fallback_full.png');
    await ctrl.screenshot({ output: fullPath });
    log(`Full screenshot saved: _verify_fallback_full.png`);
    
    console.log(JSON.stringify({
      success: false,
      error: 'template_match_failed',
      screenshot: '_verify_fallback_full.png'
    }));
    process.exit(1);
  }
  
  // Fallback C: 先截图看匹配位置是什么
  log('Fallback: Verifying match position...');
  await screenshotArea(ctrl, iconPos.x, iconPos.y, 'fallback_before');
  
  // Fallback D: 点击
  await clickAt(ctrl, iconPos.x, iconPos.y);
  await sleep(1500);
  
  // Fallback E: 截图确认
  await screenshotArea(ctrl, iconPos.x, iconPos.y, 'fallback_after');
  
  console.log(JSON.stringify({
    success: true,
    attempt: 2,
    position: iconPos,
    method: 'template_fallback',
    verify: '_verify_fallback_after.png'
  }));
  process.exit(0);
}

main().catch(err => {
  console.error(`[connect] Fatal: ${err.message}`);
  process.exit(1);
});
