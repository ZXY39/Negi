#requires -Version 5.1
<#
  Negi uninstaller (Windows).

  Started by the Windows uninstall entry (Negi-Setup.exe's wizard passes the data
  decision as a switch) and by install.ps1's own installed-apps row, and runnable
  by hand:

    powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 [-KeepData|-PurgeData] [-Yes]

  Deleting data is the only irreversible step here, so the default branch is
  "keep": the program goes, the operator's sessions, tasks and key envelopes
  stay where they are and the next install adopts them.

  This script never opens a window and never reads the console. The uninstall
  entry runs it inside a hidden process whose first GUI window is created hidden
  too, so a question asked here has no answer to wait for and the whole
  uninstallation stalls; keep/purge is decided by the caller (the wizard asks it
  in its own thread) and every line this script produces goes to a log file.
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
# %TEMP% and nowhere else: the program directory and the data root are both about to
# be deleted, and the operator's only account of a finished (or failed) uninstall has
# to outlive them.
$LogFile = Join-Path $env:TEMP ('negi-uninstall-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.log')

function Write-Step {
    param([string]$Message)
    $line = "[uninstall] $Message"
    Write-Host $line
    try {
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # A machine whose TEMP is not writable still gets the console output, and an
        # unwritable log must never be the reason an uninstall stops halfway.
    }
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
    # Two judgements, both needed: the command-line shape says what the process does,
    # the install root says whose process it is, because a second checkout on this
    # machine is not ours to stop. Beyond web/worker the list carries the desktop shell
    # (pythonw -m g3ku window) and the MCP stdio gateway: both run from this root, both
    # keep a file inside .venv image-locked, and neither has an exit endpoint that would
    # answer a graceful request.
    $rootPattern = [regex]::Escape($Root)
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and
        $_.CommandLine -and
        $_.CommandLine -match $rootPattern -and
        (
            $_.CommandLine -match 'negi_bootstrap\.py"?\s+(web|start)' -or
            $_.CommandLine -match '-m\s+g3ku\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+worker' -or
            $_.CommandLine -match '-m\s+g3ku\s+window' -or
            $_.CommandLine -match '-m\s+g3ku\s+mcp'
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
    # The graceful lane only reaches the web server. Everything still listed here was
    # picked by this install root in its own command line, so ending it is not a guess,
    # and it is the same move negi_bootstrap.py's reaper makes before a start: a locked
    # .venv is what turns "uninstalled" into "some files are still on disk".
    $pids = ($remaining | ForEach-Object { $_.ProcessId }) -join ', '
    Write-Step "graceful exit timed out; ending this install's processes (PID: $pids)"
    foreach ($proc in $remaining) {
        # taskkill writes its result to stderr in the machine's ANSI code page, and
        # `2>&1` under ErrorActionPreference=Stop turns that text into a terminating
        # error - which would abort the uninstall over a process that is already gone.
        # The exit code is the fact: 0 ended it, 128 means somebody else already did.
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & taskkill.exe /T /F /PID $proc.ProcessId 2>$null | Out-Null
        $killCode = $LASTEXITCODE
        $ErrorActionPreference = $saved
        Write-Step ("  taskkill PID {0}: exit {1}" -f $proc.ProcessId, $killCode)
    }
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        $remaining = @(Get-NegiManagedProcesses -Root $Root)
        if (-not $remaining) { return @() }
        Start-Sleep -Milliseconds 250
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
        $item = $_
        if ($KeepNames -contains $item.Name) { return }
        try {
            Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            Write-Step "removed (program entry): $($item.FullName)"
        } catch {
            # In a catch block $_ is the error record, and Set-StrictMode makes reading
            # .FullName off it a terminating error: one locked file would abort the whole
            # deletion mid-tree. Name the captured item and report the failure instead.
            Write-Step "could not remove $($item.FullName) -- $($_.Exception.Message)"
        }
    }
}

$ProgramMarkers = @('negi_bootstrap.py', '.g3ku\config.json')

if ($KeepData -and $PurgeData) {
    Write-Step '-KeepData and -PurgeData are mutually exclusive'
    exit 2
}

# The caller decides; this script has no window to ask with. The default is keep, so
# the only branch that destroys data is the one that says so explicitly. -Yes stays
# accepted because uninstall.sh's --yes means the same thing and the README documents
# one flag list for both lanes.
$decision = 'keep'
if ($PurgeData) { $decision = 'purge' }

try {
    $pointer = Read-Pointer
    $root = [string](Get-PointerValue -Pointer $pointer -Name 'root' -Default $ProjectRoot)
    $binDir = [string](Get-PointerValue -Pointer $pointer -Name 'bin_dir' -Default (Join-Path $env:USERPROFILE '.local\bin'))
    $pathEntryAdded = [bool](Get-PointerValue -Pointer $pointer -Name 'path_entry_added' -Default $false)

    Write-Step "program directory: $root"
    Write-Step "data decision: $decision"

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
        Write-Step "these Negi processes refused to end, even forced (PID: $pids). Nothing was deleted."
        Write-Step 'end them yourself and run this again.'
        exit 1
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
} catch {
    Write-Step ("FAILED: {0}" -f $_.Exception.Message)
    Write-Step 'the uninstall stopped here; whatever is listed above was already removed'
    exit 1
} finally {
    Write-Step "log: $LogFile"
}
