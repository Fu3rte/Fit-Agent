param(
    [Parameter(Mandatory = $true)][ValidateSet("snapshot", "stop")]
    [string]$Command,
    [int]$RootPid = 0,
    [string]$Ports = "8000,15173",
    [string]$SnapshotFile = "",
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
$portList = @($Ports -split "[,\s]+" | Where-Object { $_ -ne "" } | ForEach-Object { [int]$_ })

function Get-ProcessTable {
    $table = @{}
    Get-CimInstance Win32_Process | ForEach-Object {
        $table[[int]$_.ProcessId] = $_
    }
    return $table
}

function Get-DescendantProcesses {
    param([int]$RootProcessId, [hashtable]$Table)
    $found = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootProcessId)
    $seen = @{}
    while ($queue.Count -gt 0) {
        $current = [int]$queue.Dequeue()
        if ($seen.ContainsKey($current)) { continue }
        $seen[$current] = $true
        $parent = $Table[$current]
        # 父进程已退出时其 PID 可能被复用；不能按残留 ParentProcessId 认领其他进程树。
        if ($null -eq $parent) { continue }
        foreach ($item in $Table.Values) {
            if ([int]$item.ParentProcessId -ne $current) { continue }
            # 子进程创建时间必须不早于父进程，PID 复用形成的假父子关系在此排除。
            if ($null -eq $item.CreationDate -or $item.CreationDate -lt $parent.CreationDate) { continue }
            $found += $item
            $queue.Enqueue([int]$item.ProcessId)
        }
    }
    return $found
}

function Convert-CommandLine {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $null }
    $clean = $Value -replace "(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[=:]\s*\S+", '$1=<redacted>'
    $clean = $clean -replace "(?i)--api-key\s+\S+", '--api-key <redacted>'
    return $clean
}

function Get-ProcessRecord {
    param($Process)
    return [ordered]@{
        pid             = [int]$Process.ProcessId
        ppid            = [int]$Process.ParentProcessId
        creation_date   = $Process.CreationDate.ToString("o")
        executable_path = $Process.ExecutablePath
        command_line    = Convert-CommandLine $Process.CommandLine
    }
}

function Get-ListenerRecords {
    param([int[]]$Ports)
    $records = @()
    foreach ($port in $Ports) {
        $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            $records += [ordered]@{
                local_address  = $listener.LocalAddress
                local_port     = [int]$listener.LocalPort
                owning_process = [int]$listener.OwningProcess
            }
        }
    }
    return $records
}

function Write-JsonFile {
    param([string]$Path, $Value)
    $directory = Split-Path -Parent $Path
    if ($directory) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    $Value | ConvertTo-Json -Depth 8 | Set-Content -Path $Path -Encoding UTF8
}

if ($Command -eq "snapshot") {
    if ($RootPid -le 0) { throw "snapshot 需要 RootPid" }
    $table = Get-ProcessTable
    $root = $table[$RootPid]
    $processes = @()
    if ($null -ne $root) { $processes += Get-ProcessRecord $root }
    foreach ($item in (Get-DescendantProcesses -RootProcessId $RootPid -Table $table)) {
        $processes += Get-ProcessRecord $item
    }
    $payload = [ordered]@{
        captured_at = (Get-Date).ToString("o")
        root_pid    = $RootPid
        processes   = $processes
        listeners   = (Get-ListenerRecords -Ports $portList)
    }
    if ($OutFile) { Write-JsonFile -Path $OutFile -Value $payload }
    $payload | ConvertTo-Json -Depth 8
    exit 0
}

if ($Command -eq "stop") {
    if (-not $SnapshotFile -or -not (Test-Path $SnapshotFile)) { throw "stop 需要有效 SnapshotFile" }
    $snapshot = Get-Content -Path $SnapshotFile -Raw | ConvertFrom-Json
    $recorded = @($snapshot.processes)
    $results = @()
    $alive = @{}
    foreach ($item in $recorded) { $alive[[int]$item.pid] = $item }

    function Get-StopRank {
        param($Item)
        $line = [string]$Item.command_line
        if ($line -match "uvicorn" -and $line -match "--reload") { return 0 }
        if ($line -match "spawn_main" -or $line -match "multiprocessing" -or ($line -match "uvicorn" -and $line -notmatch "--reload")) { return 1 }
        if ($line -match "vite|esbuild") { return 2 }
        if ($line -match "concurrently|npm-cli|npm\.cmd|uv\.exe|uv run") { return 3 }
        return 4
    }

    $ordered = $recorded | Sort-Object `
        @{ Expression = { Get-StopRank $_ }; Ascending = $true }, `
        @{ Expression = { [int]$_.pid }; Ascending = $false }

    # 关键系统进程拒绝终止：快照误判时由断言失败暴露，不得影响当前用户会话。
    $criticalSystemNames = @(
        "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
        "services.exe", "lsass.exe", "dwm.exe", "fontdrvhost.exe", "explorer.exe"
    )

    foreach ($item in $ordered) {
        $pidValue = [int]$item.pid
        $live = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
        if ($null -eq $live) {
            $results += [ordered]@{ pid = $pidValue; outcome = "already_exited" }
            continue
        }
        $liveCreation = $live.CreationDate.ToString("o")
        if ($liveCreation -ne [string]$item.creation_date -or $live.ExecutablePath -ne [string]$item.executable_path) {
            $results += [ordered]@{ pid = $pidValue; outcome = "identity_mismatch"; live_creation = $liveCreation; live_path = $live.ExecutablePath }
            continue
        }
        $executableName = ""
        if (-not [string]::IsNullOrEmpty($live.ExecutablePath)) {
            $executableName = [System.IO.Path]::GetFileName($live.ExecutablePath)
        }
        if ($criticalSystemNames -contains $executableName) {
            $results += [ordered]@{ pid = $pidValue; outcome = "protected_skipped"; executable = $executableName }
            continue
        }
        try {
            Stop-Process -Id $pidValue -Force -ErrorAction Stop
            $deadline = (Get-Date).AddSeconds(3)
            while ((Get-Date) -lt $deadline) {
                if ($null -eq (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 50
            }
            $still = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
            $results += [ordered]@{ pid = $pidValue; outcome = $(if ($null -eq $still) { "stopped" } else { "still_alive" }) }
        }
        catch {
            $results += [ordered]@{ pid = $pidValue; outcome = "error"; error = $_.Exception.Message }
        }
    }

    $remaining = @()
    foreach ($item in $recorded) {
        $pidValue = [int]$item.pid
        $live = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
        if ($null -ne $live) {
            $remaining += [ordered]@{
                pid             = $pidValue
                creation_date   = $live.CreationDate.ToString("o")
                executable_path = $live.ExecutablePath
                command_line    = Convert-CommandLine $live.CommandLine
            }
        }
    }
    $listeners = Get-ListenerRecords -Ports $portList
    $payload = [ordered]@{
        captured_at    = (Get-Date).ToString("o")
        root_pid       = [int]$snapshot.root_pid
        results        = $results
        remaining_pids = $remaining
        listeners      = $listeners
    }
    if ($OutFile) { Write-JsonFile -Path $OutFile -Value $payload }
    $payload | ConvertTo-Json -Depth 8
    if ($remaining.Count -gt 0 -or $listeners.Count -gt 0) { exit 2 }
    exit 0
}
