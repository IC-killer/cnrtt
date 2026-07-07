<#
.SYNOPSIS
    Create Windows shortcuts (.lnk) for the cnrtt GUI tool.
.DESCRIPTION
    Creates .lnk shortcuts on the user Desktop and in the Start Menu.
    The shortcuts use wscript.exe + launch_cnrtt.vbs so the GUI launches
    without a console window.
.PARAMETER Remove
    Switch to removal mode: only remove previously created shortcuts.
.EXAMPLE
    pwsh -File scripts\create_shortcut.ps1
    pwsh -File scripts\create_shortcut.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$targetName = 'wscript.exe'
$label      = 'cnrtt RTT Viewer'
$appUserModelId = 'cnrtt.rttviewer'
$desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop'))   'cnrtt.lnk'
$startLnk   = Join-Path ([Environment]::GetFolderPath('Programs'))  'cnrtt.lnk'

function Remove-Shortcut {
    foreach ($p in @($desktopLnk, $startLnk)) {
        if (Test-Path -LiteralPath $p) {
            Remove-Item -LiteralPath $p -Force
            Write-Host ("Removed: " + $p)
        }
    }
}

function Set-ShortcutAppUserModelId {
    param(
        [Parameter(Mandatory=$true)][string]$ShortcutPath,
        [Parameter(Mandatory=$true)][string]$AppUserModelId
    )

    if (-not ('CnrttShortcutProperties' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct PropertyKey
{
    public Guid fmtid;
    public uint pid;

    public PropertyKey(Guid fmtid, uint pid)
    {
        this.fmtid = fmtid;
        this.pid = pid;
    }
}

[StructLayout(LayoutKind.Explicit, Size = 16)]
public struct PropVariant
{
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(2)] public ushort wReserved1;
    [FieldOffset(4)] public ushort wReserved2;
    [FieldOffset(6)] public ushort wReserved3;
    [FieldOffset(8)] public IntPtr pointerValue;
}

[Flags]
public enum GetPropertyStoreFlags : uint
{
    GPS_DEFAULT = 0x00000000,
    GPS_READWRITE = 0x00000002
}

public static class CnrttShortcutProperties
{
    private static readonly PropertyKey AppUserModelId =
        new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int SetValueDelegate(IntPtr self, ref PropertyKey key, ref PropVariant value);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int CommitDelegate(IntPtr self);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate uint ReleaseDelegate(IntPtr self);

    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref PropVariant propVariant);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetPropertyStoreFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string path,
        IntPtr bindContext,
        GetPropertyStoreFlags flags,
        ref Guid riid,
        out IntPtr propertyStore);

    private static void ThrowIfFailed(int hr)
    {
        if (hr < 0)
        {
            Marshal.ThrowExceptionForHR(hr);
        }
    }

    private static PropVariant PropVariantFromString(string value)
    {
        return new PropVariant
        {
            vt = 31,
            pointerValue = Marshal.StringToCoTaskMemUni(value)
        };
    }

    public static void SetAppUserModelId(string shortcutPath, string appUserModelId)
    {
        var step = "open property store";
        try
        {
            var iidPropertyStore = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
            IntPtr propertyStore;
            ThrowIfFailed(
                SHGetPropertyStoreFromParsingName(
                    shortcutPath,
                    IntPtr.Zero,
                    GetPropertyStoreFlags.GPS_READWRITE,
                    ref iidPropertyStore,
                    out propertyStore));

            try
            {
                step = "create property store delegates";
                IntPtr vtbl = Marshal.ReadIntPtr(propertyStore);
                var setValue = Marshal.GetDelegateForFunctionPointer<SetValueDelegate>(
                    Marshal.ReadIntPtr(vtbl, IntPtr.Size * 6));
                var commit = Marshal.GetDelegateForFunctionPointer<CommitDelegate>(
                    Marshal.ReadIntPtr(vtbl, IntPtr.Size * 7));

                step = "initialize AppUserModelID value";
                var key = AppUserModelId;
                PropVariant value = PropVariantFromString(appUserModelId);
                try
                {
                    step = "write AppUserModelID";
                    ThrowIfFailed(setValue(propertyStore, ref key, ref value));
                    step = "commit AppUserModelID";
                    ThrowIfFailed(commit(propertyStore));
                }
                finally
                {
                    PropVariantClear(ref value);
                }
            }
            finally
            {
                if (propertyStore != IntPtr.Zero)
                {
                    IntPtr vtbl = Marshal.ReadIntPtr(propertyStore);
                    var release = Marshal.GetDelegateForFunctionPointer<ReleaseDelegate>(
                        Marshal.ReadIntPtr(vtbl, IntPtr.Size * 2));
                    release(propertyStore);
                }
            }
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException(
                "Failed to set shortcut AppUserModelID at step: " + step
                + " (" + ex.GetType().FullName + ": " + ex.Message + ")",
                ex);
        }
    }
}
"@
    }

    [CnrttShortcutProperties]::SetAppUserModelId($ShortcutPath, $AppUserModelId)
}

if ($Remove) {
    Remove-Shortcut
    return
}

# 1. Locate wscript.exe and the no-console launcher script.
$cmdSource = Join-Path $env:WINDIR 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $cmdSource)) {
    throw ('wscript.exe not found: ' + $cmdSource)
}
$launcher = Join-Path $PSScriptRoot 'launch_cnrtt.vbs'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw ('launch_cnrtt.vbs not found: ' + $launcher)
}

# 1b. Locate the bundled icon: <site-packages>\cnrtt\assets\cnrtt.ico
$iconPath = $null
$pyExe2 = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($pyExe2) {
    $sitePkgs = Join-Path (Split-Path $pyExe2 -Parent) 'Lib\site-packages'
    $candidateIco = Join-Path $sitePkgs 'cnrtt\assets\cnrtt.ico'
    if (Test-Path -LiteralPath $candidateIco) { $iconPath = $candidateIco }
}
# Fallback to the repo copy if not installed yet
if (-not $iconPath) {
    $repoIco = Join-Path $PSScriptRoot '..\src\cnrtt\assets\cnrtt.ico'
    $repoIco = (Resolve-Path $repoIco -ErrorAction SilentlyContinue).Path
    if ($repoIco -and (Test-Path -LiteralPath $repoIco)) { $iconPath = $repoIco }
}
if (-not $iconPath) {
    Write-Warning 'cnrtt.ico not found; shortcut will use the default exe icon.'
}

# 2. Create shortcuts
$wsh = New-Object -ComObject WScript.Shell
foreach ($lnk in @($desktopLnk, $startLnk)) {
    $s = $wsh.CreateShortcut($lnk)
    $s.TargetPath       = $cmdSource
    $s.Arguments        = '"' + $launcher + '"'
    $s.WorkingDirectory = Split-Path $cmdSource -Parent
    if ($iconPath) { $s.IconLocation = $iconPath + ',0' }
    $s.Description      = $label
    $s.WindowStyle      = 1
    $s.Save()
    Set-ShortcutAppUserModelId -ShortcutPath $lnk -AppUserModelId $appUserModelId
    Write-Host ("Created: " + $lnk + "  ->  " + $cmdSource)
}

Write-Host ''
Write-Host 'Done. Double-click the cnrtt icon on the Desktop to launch the GUI.'
