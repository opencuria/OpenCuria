# Apply the desktop size the runner requested for this workspace.
#
# Runs in root's interactive session (scheduled task "OpenCuria Display", at
# logon and on demand), because display modes belong to the console session.
# Reads C:\ProgramData\opencuria\display.json {"width": W, "height": H}.
$ErrorActionPreference = 'Stop'
$result = 'error'
try {
$config = Get-Content -Raw 'C:\ProgramData\opencuria\display.json' | ConvertFrom-Json
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class OpenCuriaDisplay {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
        public int dmFields, dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
        public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
        public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2;
        public int dmPanningWidth, dmPanningHeight;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplaySettings(string device, int mode, ref DEVMODE devMode);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int ChangeDisplaySettingsEx(
        string device, ref DEVMODE devMode, IntPtr hwnd, int flags, IntPtr param);

    public static int Apply(int width, int height) {
        DEVMODE mode = new DEVMODE();
        mode.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        if (!EnumDisplaySettings(null, -1, ref mode)) { return -100; }
        if (mode.dmPelsWidth == width && mode.dmPelsHeight == height) { return 0; }
        mode.dmPelsWidth = width;
        mode.dmPelsHeight = height;
        mode.dmFields = 0x80000 | 0x100000;  // DM_PELSWIDTH | DM_PELSHEIGHT
        return ChangeDisplaySettingsEx(null, ref mode, IntPtr.Zero, 1, IntPtr.Zero);
    }
}
'@
$result = [OpenCuriaDisplay]::Apply([int] $config.width, [int] $config.height)
} catch { }
# 0 = applied; other values are DISP_CHANGE codes (e.g. -2: unsupported mode).
Set-Content -Encoding ascii -Path 'C:\ProgramData\opencuria\display.result' -Value $result
