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