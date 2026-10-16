#requires -Version 5.1
<#
  Negi uninstaller (Windows).

  Registered as the "已安装的应用" entry by install.ps1, and runnable by hand:

    powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 [-KeepData|-PurgeData] [-Yes]

  Deleting data is the only irreversible step here, so the default branch is
  "keep": the program goes, the operator's sessions, tasks and key envelopes
  stay where they are and the next install adopts them.
#>
param(
    [switch]$KeepData,
    [switch]$PurgeData,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$AppDisplayName = 'Negi'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $env:LOCALAPPDATA $AppDisplayName
$PointerFile = Join-Path $StateDir 'install-root.json'
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppDisplayName"

function Write-Step {
    param([string]$Message)
    Write-Host "[uninstall] $Message"
}

function Read-Pointer {
    # The stub resolves the install root from this file, so it is also the
    # record of what this install added to PATH.
    if (-not (Test-Path $PointerFile)) { return $null }
    try { return Get-Content -LiteralPath $PointerFile -Raw | ConvertFrom-Json } catch { return $null }
}

function Get-PointerValue {
    param($Pointer, [string]$Name, $Default)
    # StrictMode turns a missing property into an error, and a pointer written by
    # an older install may legitimately lack a field.
    if (-not $Pointer) { return $Default }
    $property = $Pointer.PSObject.Properties[$Name]
    if (-not $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Get-TreeSizeBytes {
    param([string]$Path)
    $total = 0
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($Path)
    while ($stack.Count -gt 0) {
        $entries = @()
        try { $entries = [System.IO.Directory]::EnumerateFileSystemEntries($stack.Pop()) } catch { continue }
        foreach ($entry in $entries) {
            try {
                if ([System.IO.Directory]::Exists($entry)) { $stack.Push($entry) }
                else { $total += (New-Object System.IO.FileInfo($entry)).Length }
            } catch { }
        }
    }
    return $total
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N1f} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1f} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Get-WebPort {
    param([string]$Root)
    $configPath = Join-Path $Root '.g3ku\config.json'
    if (-not (Test-Path $configPath)) { return 18790 }
    try {
        $payload = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        $port = [int]$payload.web.port
        if ($port -gt 0) { return $port }
    } catch { }
    return 18790
}

function Get-NegiManagedProcesses {
    param([string]$Root)
    $rootPattern = [regex]::Escape($Root)
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and
        $_.CommandLine -and
        $_.CommandLine -match $rootPattern -and
        (
            $_.CommandLine -match 'negi_bootstrap\.py"?\s+(web|start)' -or
            $_.CommandLine -match '-m\s+g3ku\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+worker'
        )
    }
}

function Stop-NegiRuntime {
    param([string]$Root, [int]$Port)
    $remaining = @(Get-NegiManagedProcesses -Root $Root)
    if (-not $remaining) { return @() }
    Write-Step 'requesting a graceful exit from the running Negi...'
    try {
        Invoke-RestMethod `
            -Uri "http://127.0.0.1:$Port/api/bootstrap/exit" `
            -Method Post -ContentType 'application/json' `
            -Body '{"pause_running_work":true}' -TimeoutSec 20 -ErrorAction Stop | Out-Null
    } catch {
        Write-Step 'the exit endpoint did not answer; waiting anyway'
    }
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline) {
        $remaining = @(Get-NegiManagedProcesses -Root $Root)
        if (-not $remaining) { return @() }
        Start-Sleep -Milliseconds 500
    }
    return $remaining
}

function Assert-DeletablePath {
    param([string]$Path, [string[]]$Markers, [string]$For)
    # Only ever delete a directory we can prove belongs to Negi: the guard is a
    # marker file, not a name match, because a mistyped path here is unrecoverable.
    if (-not $Path) { throw "[uninstall] refusing to delete an empty path ($For)" }
    $full = (Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue)
    if (-not $full) { throw "[uninstall] refusing to delete a missing path ($For): $Path" }
    $normalized = $full.Path.TrimEnd('\')
    if ($normalized.Length -le 3) { throw "[uninstall] refusing to delete a root-level path ($For): $normalized" }
    if ($normalized -ieq $env:USERPROFILE.TrimEnd('\')) { throw "[uninstall] refusing to delete the user profile ($For)" }
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
        if ($normalized -ieq $drive.Root.TrimEnd('\')) { throw "[uninstall] refusing to delete a drive root ($For)" }
    }
    foreach ($marker in $Markers) {
        if (Test-Path -LiteralPath (Join-Path $normalized $marker)) { return $normalized }
    }
    throw "[uninstall] refusing to delete ${normalized}: no Negi marker found ($For)"
}

function Remove-Tree {
    param([string]$Path, [string]$For)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        Write-Step "removed ($For): $Path"
    } catch {
        Write-Step "could not remove ($For): $Path -- $($_.Exception.Message)"
    }
}

function Assert-DeletableDataRoot {
    param([string]$Path)
    # The data root is named by env / pointer / default, so a mistyped value is
    # the failure to guard against. Two independent facts must hold: a Negi
    # config or key envelope, and at least one runtime directory.
    $normalized = Assert-DeletablePath -Path $Path -Markers @('config.json', 'llm-config') -For 'data root'
    foreach ($dir in @('logs', 'sessions', 'tasks', 'llm-config', 'memory', 'runtime')) {
        if (Test-Path -LiteralPath (Join-Path $normalized $dir)) { return $normalized }
    }
    throw "[uninstall] refusing to delete ${normalized}: no Negi runtime directory found (data root)"
}

function Remove-ProgramKeepingData {
    param([string]$Path, [string[]]$KeepNames)
    # "Keep data" has to survive its own recursive delete: the data root lives
    # inside the program directory on a default install, so only the code goes.
    Get-ChildItem -LiteralPath $Path -Force | ForEach-Object {
        if ($KeepNames -contains $_.Name) { return }
        try {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Step "could not remove $($_.FullName) -- $($_.Exception.Message)"
        }
    }
}

function Ask-KeepOrPurge {
    param([string]$DataPath, [string]$DataSize, [string]$ProgramSize)
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    # Click handlers run in the script scope, so the answer is carried on a
    # script-scope name; a function-local would stay 'cancel' whatever was clicked.
    $script:NegiUninstallChoice = 'cancel'
    $form = New-Object System.Windows.Forms.Form
    $form.Text = '卸载 Negi'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object System.Drawing.Size(520, 210)
    $label = New-Object System.Windows.Forms.Label
    $label.Location = New-Object System.Drawing.Point(18, 16)
    $label.Size = New-Object System.Drawing.Size(484, 96)
    $label.Text = "Negi 将从这台电脑卸载。`r`n`r`n程序本体：$ProgramSize`r`n会话、任务与配置数据：$DataSize`r`n$DataPath`r`n`r`n保留数据：只删程序，下次安装直接认领这些数据。`r`n全部删除：连同上面的目录一起删掉，无法恢复。"
    $form.Controls.Add($label)
    $keep = New-Object System.Windows.Forms.Button
    $keep.Text = '保留数据并卸载'
    $keep.Location = New-Object System.Drawing.Point(18, 150)
    $keep.Size = New-Object System.Drawing.Size(150, 34)
    $keep.Add_Click({ $script:NegiUninstallChoice = 'keep'; $this.Parent.Close() })
    $purge = New-Object System.Windows.Forms.Button
    $purge.Text = '全部删除'
    $purge.Location = New-Object System.Drawing.Point(186, 150)
    $purge.Size = New-Object System.Drawing.Size(150, 34)
    $purge.Add_Click({ $script:NegiUninstallChoice = 'purge'; $this.Parent.Close() })
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = '取消'
    $cancel.Location = New-Object System.Drawing.Point(354, 150)
    $cancel.Size = New-Object System.Drawing.Size(148, 34)
    $cancel.Add_Click({ $script:NegiUninstallChoice = 'cancel'; $this.Parent.Close() })
    $form.Controls.Add($keep)
    $form.Controls.Add($purge)
    $form.Controls.Add($cancel)
    [void]$form.ShowDialog()
    $form.Dispose()
    return $script:NegiUninstallChoice
}

$ProgramMarkers = @('negi_bootstrap.py', '.g3ku\config.json')

$pointer = Read-Pointer
$root = [string](Get-PointerValue -Pointer $pointer -Name 'root' -Default $ProjectRoot)
$binDir = [string](Get-PointerValue -Pointer $pointer -Name 'bin_dir' -Default (Join-Path $env:USERPROFILE '.local\bin'))
$pathEntryAdded = [bool](Get-PointerValue -Pointer $pointer -Name 'path_entry_added' -Default $false)

Write-Step "program directory: $root"

$dataDir = Join-Path $root '.g3ku'
$dataEnv = "$env:G3KU_DATA_DIR".Trim()
if ($dataEnv -and (Test-Path -LiteralPath $dataEnv)) { $dataDir = $dataEnv }
elseif (Test-Path -LiteralPath (Join-Path $root '.g3ku\data-root.json')) {
    try {
        $pointed = (Get-Content -LiteralPath (Join-Path $root '.g3ku\data-root.json') -Raw | ConvertFrom-Json).data_dir
        if ($pointed -and (Test-Path -LiteralPath $pointed)) { $dataDir = [string]$pointed }
    } catch { }
}

$port = Get-WebPort -Root $root
$leftover = Stop-NegiRuntime -Root $root -Port $port
if ($leftover) {
    $pids = ($leftover | ForEach-Object { $_.ProcessId }) -join ', '
    Write-Step "Negi processes are still running (PID: $pids). Nothing was deleted."
    Write-Step 'close them from the web UI or end those processes, then run this again.'
    exit 1
}

$programBytes = Get-TreeSizeBytes -Path $root
$dataBytes = 0
if (Test-Path -LiteralPath $dataDir) { $dataBytes = Get-TreeSizeBytes -Path $dataDir }

$decision = 'cancel'
if ($KeepData -and $PurgeData) {
    Write-Step '-KeepData and -PurgeData are mutually exclusive'
    exit 2
}
if ($KeepData) { $decision = 'keep' }
elseif ($PurgeData) { $decision = 'purge' }
elseif ($Yes) { $decision = 'keep' }
else {
    $decision = Ask-KeepOrPurge -DataPath $dataDir -DataSize (Format-Size $dataBytes) -ProgramSize (Format-Size $programBytes)
}
if ($decision -eq 'cancel') {
    Write-Step 'cancelled; nothing was deleted'
    exit 0
}

# Entry surface first: these live outside the program directory.
Remove-Tree -Path (Join-Path $binDir 'negi.cmd') -For 'command stub'
Remove-Tree -Path (Join-Path $binDir 'negi-root.cmd') -For 'install root pointer'
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    if ($folder) { Remove-Tree -Path (Join-Path $folder "$AppDisplayName.lnk") -For 'shortcut' }
}
Remove-Tree -Path $StateDir -For 'icon and pointer state'

if ($pathEntryAdded) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($current) { $parts = @($current -split ';' | Where-Object { $_ }) }
    $wanted = $binDir.TrimEnd('\')
    $kept = @($parts | Where-Object { $_.TrimEnd('\') -ine $wanted })
    if ($kept.Count -ne $parts.Count) {
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
        Write-Step "removed $binDir from the user PATH (this install added it)"
    }
}

if (Test-Path $UninstallKey) {
    Remove-Item -Path $UninstallKey -Force -ErrorAction SilentlyContinue
    Write-Step 'removed the installed-apps entry'
}

if ($decision -eq 'purge') {
    $dataFull = Assert-DeletableDataRoot -Path $dataDir
    $rootFull = Assert-DeletablePath -Path $root -Markers $ProgramMarkers -For 'program directory'
    if (-not $dataFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Tree -Path $dataFull -For 'data root'
    }
}

Assert-DeletablePath -Path $root -Markers $ProgramMarkers -For 'program directory' | Out-Null
if ($decision -eq 'purge') {
    Remove-Tree -Path $root -For 'program directory'
} else {
    Remove-ProgramKeepingData -Path $root -KeepNames @('.g3ku')
}

if ($decision -eq 'keep') {
    Write-Step "kept the data root: $dataDir (the next install into $root adopts it)"
} else {
    Write-Step 'program and data removed'
}
Write-Step 'uv and the Python it manages were left alone (shared toolchain)'
Write-Step 'done'
exit 0
