Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FocusAPI {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
$hwnd = [IntPtr]::new(1057106)
[FocusAPI]::ShowWindow($hwnd, 9) | Out-Null
[FocusAPI]::SetForegroundWindow($hwnd) | Out-Null
Write-Output "focused"