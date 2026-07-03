<#
.SYNOPSIS
    Create Windows shortcuts (.lnk) for the cnrtt GUI tool.
.DESCRIPTION
    Finds the installed cnrtt-gui.exe (Python gui-scripts entry point)
    and creates a .lnk shortcut on the user Desktop and in the Start Menu,
    so the GUI can be launched by double-clicking without opening a terminal.
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

$targetName = 'cnrtt-gui.exe'
$label      = 'cnrtt RTT Viewer'
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

if ($Remove) {
    Remove-Shortcut
    return
}

# 1. Locate cnrtt-gui.exe: prefer PATH, else fall back to current Python Scripts dir
$cmd = Get-Command $targetName -ErrorAction SilentlyContinue
$cmdSource = $null
if (-not $cmd) {
    $pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($pyExe) {
        $scriptsDir = Join-Path (Split-Path $pyExe -Parent) 'Scripts'
        $candidate  = Join-Path $scriptsDir $targetName
        if (Test-Path -LiteralPath $candidate) { $cmdSource = $candidate }
    }
    if (-not $cmdSource) {
        throw 'cnrtt-gui.exe not found. Run: pip install .  first.'
    }
} else {
    $cmdSource = $cmd.Source
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
    $s.WorkingDirectory = Split-Path $cmdSource -Parent
    if ($iconPath) { $s.IconLocation = $iconPath + ',0' }
    $s.Description      = $label
    # 7 = Minimized; the console flashes briefly then the GUI shows on its own.
    $s.WindowStyle      = 7
    $s.Save()
    Write-Host ("Created: " + $lnk + "  ->  " + $cmdSource)
}

Write-Host ''
Write-Host 'Done. Double-click the cnrtt icon on the Desktop to launch the GUI.'
