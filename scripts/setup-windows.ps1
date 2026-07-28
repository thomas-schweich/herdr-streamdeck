<#
.SYNOPSIS
    Set up USB passthrough of an Elgato Stream Deck from Windows into WSL2,
    and register a logon task that keeps it attached automatically.

.DESCRIPTION
    Run from an ADMINISTRATOR PowerShell. Admin is needed only for the one-time
    'usbipd bind'; the logon task itself runs unelevated.

    Run scripts/setup-wsl.sh inside WSL FIRST, so the udev rule exists before
    the device appears.

.PARAMETER BusId
    Override device auto-detection (e.g. "1-4"). Use if you have several
    Elgato devices and want a specific one.

.PARAMETER TaskName
    Name of the scheduled task. Default: "usbipd-attach-streamdeck".

.PARAMETER Remove
    Unregister the scheduled task and unbind the device.

.PARAMETER VisibleConsole
    Run the logon task in a normal console window instead of hiding it via
    'conhost --headless'. Use this if the task shows as running but the device
    never attaches -- it makes the failure visible.

.PARAMETER LogPath
    Transcribe all output to this file. Used when launched elevated from WSL by
    scripts/attach_device.py: 'Start-Process -Verb RunAs' cannot be combined
    with output redirection, so a transcript is the only way for the calling
    side to see what happened. Also keeps the window's output after it closes.

.EXAMPLE
    .\setup-windows.ps1
    .\setup-windows.ps1 -BusId 2-1
    .\setup-windows.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string] $BusId,
    [string] $TaskName = 'usbipd-attach-streamdeck',
    [switch] $Remove,
    [switch] $VisibleConsole,
    [string] $LogPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Transcribing = $false
if ($LogPath) {
    try {
        Start-Transcript -Path $LogPath -Force | Out-Null
        $script:Transcribing = $true
    } catch {
        Write-Warning "could not start transcript at ${LogPath}: $($_.Exception.Message)"
    }
}

function Stop-Logging {
    if ($script:Transcribing) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:Transcribing = $false
    }
}

$ELGATO_VID = '0FD9'

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  ok $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "warn $m" -ForegroundColor Yellow }
function Write-Fail {
    param($m)
    Write-Host "fail $m" -ForegroundColor Red
    Stop-Logging
    # When launched elevated from WSL the window closes instantly, so give the
    # reader a moment; the transcript is the durable record either way.
    if ($LogPath) { Start-Sleep -Seconds 5 }
    exit 1
}

# --------------------------------------------------------------- prerequisites

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Fail 'Must run from an Administrator PowerShell ("usbipd bind" requires it).'
    }
}

function Update-PathFromRegistry {
    # winget-installed binaries are not on PATH in an already-open shell.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($machine, $user | Where-Object { $_ }) -join ';'
}

function Get-UsbipdPath {
    $cmd = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $fallback = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
    if (Test-Path $fallback) { return $fallback }
    return $null
}

function Install-Usbipd {
    Write-Step 'Checking for usbipd-win'
    if (Get-UsbipdPath) {
        Write-Ok "already installed ($(Get-UsbipdPath))"
        return
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Fail 'winget not found. Install usbipd-win manually: https://github.com/dorssel/usbipd-win/releases'
    }

    Write-Step 'Installing usbipd-win via winget'
    winget install --exact --id dorssel.usbipd-win `
        --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { Write-Fail "winget install failed (exit $LASTEXITCODE)" }

    Update-PathFromRegistry
    if (-not (Get-UsbipdPath)) {
        Write-Fail 'usbipd installed but not found on PATH. Open a new Administrator PowerShell and re-run.'
    }
    Write-Ok 'usbipd-win installed'
}

function Assert-WslReady {
    Write-Step 'Checking WSL'
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Fail 'wsl.exe not found.'
    }
    # wsl -l -v emits UTF-16; decode so the match works.
    $prev = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.Encoding]::Unicode
        $distros = (wsl.exe -l -v) -join "`n"
    } finally {
        [Console]::OutputEncoding = $prev
    }
    if ($distros -notmatch '\s2\s*$' -and $distros -notmatch '\s2\s') {
        Write-Warn 'No WSL 2 distro detected. usbip passthrough requires WSL 2, not WSL 1.'
    } else {
        Write-Ok 'WSL 2 distro present'
    }
}

# ------------------------------------------------------------ device discovery

function Find-StreamDeck {
    param([string] $Usbipd)

    Write-Step "Looking for an Elgato device (VID $ELGATO_VID)"

    $devices = @()

    # 'usbipd state' emits JSON and is far more robust than scraping 'list'.
    # Property access is guarded: under StrictMode a missing key throws, and
    # the JSON shape differs across usbipd versions.
    try {
        $state = & $Usbipd state 2>$null | ConvertFrom-Json
        $entries = if ($state.PSObject.Properties.Name -contains 'Devices') { $state.Devices } else { @() }
        foreach ($d in $entries) {
            $props = $d.PSObject.Properties.Name
            $instanceId = if ($props -contains 'InstanceId') { $d.InstanceId } else { '' }
            if ($instanceId -notmatch "VID_$ELGATO_VID") { continue }
            $devices += [pscustomobject]@{
                BusId       = if ($props -contains 'BusId')       { $d.BusId }       else { '' }
                Description = if ($props -contains 'Description') { $d.Description } else { 'Elgato device' }
                Persisted   = ($props -contains 'PersistedGuid') -and $d.PersistedGuid
            }
        }
        # A bound-but-detached device has no BusId; it cannot be attached.
        $devices = @($devices | Where-Object { $_.BusId })
    } catch {
        Write-Warn "'usbipd state' unavailable, falling back to text parsing"
    }

    # Fallback for older usbipd builds: parse the VID:PID column of 'list'.
    if ($devices.Count -eq 0) {
        foreach ($line in (& $Usbipd list 2>$null)) {
            if ($line -match '^\s*(?<bus>\d+-\d+)\s+(?<vidpid>[0-9a-f]{4}:[0-9a-f]{4})\s+(?<desc>.*?)\s\s+') {
                if ($Matches.vidpid -like "$($ELGATO_VID.ToLower()):*") {
                    $devices += [pscustomobject]@{
                        BusId       = $Matches.bus
                        Description = $Matches.desc.Trim()
                        Persisted   = $false
                    }
                }
            }
        }
    }

    if ($devices.Count -eq 0) {
        Write-Host ''
        Write-Host '  No Elgato device found. Plug the Stream Deck in and re-run.'
        Write-Host '  Current devices:'
        & $Usbipd list
        Write-Fail 'no device to bind'
    }

    if ($devices.Count -gt 1) {
        Write-Warn "Multiple Elgato devices found; pass -BusId to choose:"
        $devices | ForEach-Object { Write-Host "    $($_.BusId)  $($_.Description)" }
        Write-Fail 'ambiguous device selection'
    }

    Write-Ok "found $($devices[0].BusId) -- $($devices[0].Description)"
    return $devices[0].BusId
}

function Set-DeviceBound {
    param([string] $Usbipd, [string] $Id)

    Write-Step "Binding $Id (one-time; persists across reboots)"
    & $Usbipd bind --busid $Id 2>&1 | ForEach-Object {
        # Re-binding an already-bound device is a no-op that warns; not an error.
        if ($_ -match 'already' ) { Write-Ok 'already bound' } else { Write-Host "    $_" }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "bind returned exit $LASTEXITCODE -- if the device is claimed by another driver, try: $Usbipd bind --force --busid $Id"
    } else {
        Write-Ok "bound $Id"
    }
}

# ----------------------------------------------------------- scheduled task

function Register-AttachTask {
    param([string] $Usbipd, [string] $Id)

    Write-Step "Registering logon task '$TaskName'"

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok 'replaced existing task'
    }

    # --auto-attach blocks forever, re-attaching on replug and on WSL restart.
    # It is a console app, so a plain task action leaves a window open for the
    # whole session. conhost --headless suppresses that; -VisibleConsole opts
    # out if it ever misbehaves (the failure mode is invisible otherwise).
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    if (-not $VisibleConsole -and (Test-Path $conhost)) {
        $execute  = $conhost
        $argument = "--headless `"$Usbipd`" attach --wsl --busid $Id --auto-attach"
        Write-Ok 'window hidden via conhost --headless'
    } else {
        $execute  = $Usbipd
        $argument = "attach --wsl --busid $Id --auto-attach"
        Write-Warn 'task will show a console window for the session'
    }

    $action = New-ScheduledTaskAction -Execute $execute -Argument $argument

    # AtLogOn, not AtStartup: 'attach --wsl' targets the *user's* WSL VM, which
    # does not exist before login. A SYSTEM task at boot lands in session 0 and
    # cannot reach the distro.
    $user    = "$env:USERDOMAIN\$env:USERNAME"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Give WSL and the USB stack a moment to settle before attaching.
    $trigger.Delay = 'PT20S'

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)   # 0 == run indefinitely

    # Deliberately NOT setting $settings.Hidden -- that hides the task from the
    # Task Scheduler UI (it does nothing to the console window) and makes this
    # much harder to debug later.

    # Limited (unelevated): 'attach' needs no admin rights, only 'bind' did.
    $principal = New-ScheduledTaskPrincipal -UserId $user `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description 'Keeps the Elgato Stream Deck attached to WSL2 via usbipd.' | Out-Null

    Write-Ok "task registered (runs as $user at logon, +20s delay)"
}

function Remove-Setup {
    param([string] $Usbipd)

    Write-Step "Removing task '$TaskName'"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask  -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok 'task removed'
    } else {
        Write-Ok 'no task registered'
    }

    if (-not $Usbipd) { return }

    # Do not let a missing device abort the teardown -- the task is already gone
    # and unbinding an absent device is not worth failing over.
    $id = $BusId
    if (-not $id) {
        $state = & $Usbipd state 2>$null | ConvertFrom-Json
        $entries = if ($state -and $state.PSObject.Properties.Name -contains 'Devices') { $state.Devices } else { @() }
        $match = $entries | Where-Object {
            ($_.PSObject.Properties.Name -contains 'InstanceId') -and $_.InstanceId -match "VID_$ELGATO_VID"
        } | Select-Object -First 1
        if ($match -and $match.BusId) { $id = $match.BusId }
    }

    if (-not $id) {
        Write-Warn 'no Elgato device found to unbind (already unplugged or unbound)'
        return
    }

    Write-Step "Detaching and unbinding $id"
    & $Usbipd detach --busid $id 2>&1 | Out-Null
    & $Usbipd unbind --busid $id 2>&1 | Out-Null
    Write-Ok 'unbound'
}

# ------------------------------------------------------------------------ main

Assert-Admin
Update-PathFromRegistry

if ($Remove) {
    Remove-Setup -Usbipd (Get-UsbipdPath)
    Write-Host ''
    Write-Ok 'Removed.'
    Stop-Logging
    exit 0
}

Install-Usbipd
Assert-WslReady

$usbipd = Get-UsbipdPath
if (-not $BusId) { $BusId = Find-StreamDeck -Usbipd $usbipd }

Set-DeviceBound   -Usbipd $usbipd -Id $BusId
Register-AttachTask -Usbipd $usbipd -Id $BusId

Write-Step 'Starting the task now (so you need not log out)'
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq 'Running') {
    Write-Ok 'task is running'
} else {
    Write-Warn "task state is '$state' -- check Task Scheduler history"
}

Write-Host ''
Write-Ok 'Windows side ready.'
Write-Host ''
Write-Host '  Verify from inside WSL:' -ForegroundColor Cyan
Write-Host '      ./scripts/setup-wsl.sh --verify-only'
Write-Host ''
Write-Host '  Undo everything:' -ForegroundColor Cyan
Write-Host "      .\scripts\setup-windows.ps1 -Remove"

Stop-Logging
