param(
    [ValidateSet("all", "job-api", "control-panel", "public-entry")]
    [string]$Service = "all",
    [string]$ConfigPath = "",
    [switch]$CheckOnly
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$defaultConfigPath = Join-Path $projectRoot "remote_workbench.local.json"
$candidateConfigPath = if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $defaultConfigPath
} else {
    $ConfigPath
}

if (-not (Test-Path $candidateConfigPath)) {
    throw "Remote workbench config not found: $candidateConfigPath"
}
$resolvedConfigPath = (Resolve-Path $candidateConfigPath).Path

$pythonCommand = Get-Command python -ErrorAction Stop
$pythonExe = (& $pythonCommand.Source -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $pythonExe = $pythonCommand.Source
}
$pythonExe = [string]$pythonExe
$pythonExe = $pythonExe.Trim()
$serviceRunnerPath = Join-Path $projectRoot "scripts\run_remote_workbench_service.py"
$config = Get-Content -Raw -Encoding UTF8 $resolvedConfigPath | ConvertFrom-Json
$runtimeLogsValue = if ($config.runtime_logs -and $config.runtime_logs.directory) {
    [string]$config.runtime_logs.directory
} else {
    "runtime_logs"
}

if ([System.IO.Path]::IsPathRooted($runtimeLogsValue)) {
    $runtimeLogsDir = $runtimeLogsValue
} else {
    $runtimeLogsDir = Join-Path $projectRoot $runtimeLogsValue
}

New-Item -ItemType Directory -Force -Path $runtimeLogsDir | Out-Null

function Invoke-PublicEntryPreflight {
    param(
        [string]$Reason = "before background launch"
    )

    $arguments = @(
        $serviceRunnerPath,
        "public-entry",
        "--config",
        $resolvedConfigPath,
        "--check-only"
    )

    Write-Host "Running public-entry preflight ($Reason)..."
    & $pythonExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Public-entry preflight failed. Fix the blocking items above, then rerun with -Service public-entry -CheckOnly or start again after remediation."
    }
}

function Get-ListeningProcessInfo {
    param(
        [int]$Port
    )

    $listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq $Port } |
        Select-Object -First 1
    if (-not $listener) {
        return $null
    }

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        pid = $listener.OwningProcess
        local_address = $listener.LocalAddress
        port = $Port
        process_name = [string]$processInfo.Name
        command_line = [string]$processInfo.CommandLine
    }
}

function Format-ListeningProcessInfo {
    param(
        [object]$ProcessInfo
    )

    if (-not $ProcessInfo) {
        return "none"
    }

    $commandLine = [string]$ProcessInfo.command_line
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        $commandLine = "<unavailable>"
    }

    "PID $($ProcessInfo.pid) [$($ProcessInfo.process_name)] on $($ProcessInfo.local_address):$($ProcessInfo.port); command: $commandLine"
}

function Get-ServiceStartupMarker {
    param(
        [string]$ServiceName
    )

    switch ($ServiceName) {
        "job-api" { return "Job API started at" }
        "control-panel" { return "Control panel started at" }
        "public-entry" { return "Public entry started at" }
        default { return "" }
    }
}

function Get-ServiceConfiguredPort {
    param(
        [string]$ServiceName
    )

    switch ($ServiceName) {
        "job-api" { return [int]$config.job_api.port }
        "control-panel" {
            if ($config.control_panel -and $config.control_panel.binding -and $config.control_panel.binding.port) {
                return [int]$config.control_panel.binding.port
            }
            return 0
        }
        default { return 0 }
    }
}

function Get-ServiceStartupFailureDetail {
    param(
        [string]$ServiceName,
        [object]$ServiceInfo,
        [int]$Port
    )

    $stdoutPath = [string]$ServiceInfo.stdout_log
    $stderrPath = [string]$ServiceInfo.stderr_log
    $stdoutText = if (-not [string]::IsNullOrWhiteSpace($stdoutPath) -and (Test-Path $stdoutPath)) {
        Get-Content -Raw -Encoding UTF8 $ServiceInfo.stdout_log
    } else {
        ""
    }
    $stderrText = if (-not [string]::IsNullOrWhiteSpace($stderrPath) -and (Test-Path $stderrPath)) {
        Get-Content -Raw -Encoding UTF8 $ServiceInfo.stderr_log
    } else {
        ""
    }
    $listenerInfo = if ($Port -gt 0) {
        Get-ListeningProcessInfo -Port $Port
    } else {
        $null
    }
    $detailParts = @()
    if ($listenerInfo) {
        $detailParts += "Current listener: $(Format-ListeningProcessInfo -ProcessInfo $listenerInfo)"
    }
    if (-not [string]::IsNullOrWhiteSpace($stderrText)) {
        $detailParts += "stderr: $($stderrText.Trim())"
    }
    if (-not [string]::IsNullOrWhiteSpace($stdoutText)) {
        $detailParts += "stdout: $($stdoutText.Trim())"
    }
    if ($detailParts.Count -eq 0) {
        return "No additional startup detail was captured."
    }
    return ($detailParts -join " ")
}

function Wait-WorkbenchServiceStartup {
    param(
        [object]$ServiceInfo,
        [int]$TimeoutSeconds = 15
    )

    $startupMarker = Get-ServiceStartupMarker -ServiceName $ServiceInfo.service
    if ([string]::IsNullOrWhiteSpace($startupMarker)) {
        return
    }

    $configuredPort = Get-ServiceConfiguredPort -ServiceName $ServiceInfo.service
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        $stdoutText = if (Test-Path $ServiceInfo.stdout_log) {
            Get-Content -Raw -Encoding UTF8 $ServiceInfo.stdout_log
        } else {
            ""
        }
        if ($stdoutText -like "*$startupMarker*") {
            return
        }

        $process = Get-Process -Id $ServiceInfo.pid -ErrorAction SilentlyContinue
        if (-not $process) {
            $detail = Get-ServiceStartupFailureDetail -ServiceName $ServiceInfo.service -ServiceInfo $ServiceInfo -Port $configuredPort
            throw "Service '$($ServiceInfo.service)' exited before confirming startup. $detail"
        }

        Start-Sleep -Milliseconds 300
    }

    $timeoutDetail = Get-ServiceStartupFailureDetail -ServiceName $ServiceInfo.service -ServiceInfo $ServiceInfo -Port $configuredPort
    throw "Service '$($ServiceInfo.service)' did not confirm startup within $TimeoutSeconds seconds. $timeoutDetail"
}

function Stop-StartedWorkbenchServices {
    param(
        [object[]]$ServicesToStop
    )

    foreach ($serviceInfo in ($ServicesToStop | Sort-Object -Property service -Descending)) {
        try {
            Stop-Process -Id $serviceInfo.pid -Force -ErrorAction Stop
        } catch {
            continue
        }
    }
}

function Start-WorkbenchService {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ServiceName
    )

    $stdoutPath = Join-Path $runtimeLogsDir "$ServiceName.stdout.log"
    $stderrPath = Join-Path $runtimeLogsDir "$ServiceName.stderr.log"
    $arguments = @(
        "-u",
        $serviceRunnerPath,
        $ServiceName,
        "--config",
        $resolvedConfigPath
    )

    $process = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    [PSCustomObject]@{
        service = $ServiceName
        pid = $process.Id
        stdout_log = $stdoutPath
        stderr_log = $stderrPath
    }
}

$publicEntryEnabled = $config.public_entry -and $config.public_entry.enabled -eq $true
$requiresPublicEntryPreflight = $Service -eq "public-entry" -or ($Service -eq "all" -and $publicEntryEnabled)

if ($CheckOnly) {
    if ($Service -notin @("all", "public-entry")) {
        throw "-CheckOnly is only supported with -Service public-entry or -Service all."
    }
    if (-not $requiresPublicEntryPreflight) {
        throw "No enabled public-entry service is configured for this command. Enable public_entry.enabled=true or use -Service public-entry with a valid public-entry config."
    }
    Invoke-PublicEntryPreflight -Reason "check-only"
    Write-Host "Public-entry preflight completed. No background services were started."
    return
}

if ($requiresPublicEntryPreflight) {
    Invoke-PublicEntryPreflight -Reason "before background launch"
}

$startedServices = @()

try {
    switch ($Service) {
        "all" {
            $jobApiService = Start-WorkbenchService -ServiceName "job-api"
            $startedServices += $jobApiService
            Wait-WorkbenchServiceStartup -ServiceInfo $jobApiService

            if ($config.control_panel -and $config.control_panel.enabled -eq $true) {
                $controlPanelService = Start-WorkbenchService -ServiceName "control-panel"
                $startedServices += $controlPanelService
                Wait-WorkbenchServiceStartup -ServiceInfo $controlPanelService
            }
            if ($config.public_entry -and $config.public_entry.enabled -eq $true) {
                $publicEntryService = Start-WorkbenchService -ServiceName "public-entry"
                $startedServices += $publicEntryService
                Wait-WorkbenchServiceStartup -ServiceInfo $publicEntryService
            }
        }
        default {
            $serviceInfo = Start-WorkbenchService -ServiceName $Service
            $startedServices += $serviceInfo
            Wait-WorkbenchServiceStartup -ServiceInfo $serviceInfo
        }
    }
} catch {
    if ($startedServices.Count -gt 0) {
        Write-Warning "Startup failed. Stopping services started by this invocation."
        Stop-StartedWorkbenchServices -ServicesToStop $startedServices
    }
    throw
}

Write-Host "Started services:"
$startedServices | Format-Table -AutoSize
Write-Host ""
Write-Host "Runtime logs dir: $runtimeLogsDir"
Write-Host "Remote workbench config: $resolvedConfigPath"
if ($config.public_entry -and $config.public_entry.enabled -eq $true) {
    Write-Host "Public entry is enabled. Check public-entry.stdout.log, public-entry.stderr.log, and public-entry.access.log for diagnostics."
} else {
    Write-Host "Public entry is disabled in remote_workbench.local.json."
}
