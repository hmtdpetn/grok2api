$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dockerCliDir = Join-Path $env:USERPROFILE 'docker-cli\docker'
$dockerCli = Join-Path $dockerCliDir 'docker.exe'
$dockerHostFile = Join-Path $projectRoot '.docker-host'

# ── Helper functions ──────────────────────────────────────────────────────────

function Show-LauncherError {
    param([string]$Message)
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $Message,
        'Grok2API Launcher (Anti-Ban Docker)',
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
}

function Test-Grok2API {
    param([string]$HostAddr)
    try {
        $response = Invoke-WebRequest -Uri "http://${HostAddr}:8000/admin/login" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

function Test-DockerHost {
    param([string]$HostAddr)
    $savedPath = $env:PATH
    $env:PATH = "$savedPath;$dockerCliDir"
    try {
        $env:DOCKER_HOST = "tcp://${HostAddr}:2375"
        $result = & $dockerCli ps 2>&1
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $env:PATH = $savedPath
    }
}

# ── Discover Docker host ──────────────────────────────────────────────────────

function Find-DockerHost {
    # 1) Saved address from last successful run.
    if (Test-Path $dockerHostFile) {
        $cached = (Get-Content $dockerHostFile -Raw).Trim()
        if ($cached -and (Test-DockerHost $cached)) {
            Write-Host "Using cached Docker host: $cached"
            return $cached
        }
    }

    # 2) Environment variable (if set outside this script).
    $envHost = $env:DOCKER_HOST -replace '^tcp://', '' -replace ':\d+$', ''
    if ($envHost -and (Test-DockerHost $envHost)) {
        Write-Host "Using DOCKER_HOST env: $envHost"
        return $envHost
    }

    # 3) Scan local subnet for Docker Engine on port 2375.
    #    Use direct TCP connect (much faster than ping → test).
    $myIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.InterfaceAlias -notmatch 'Loopback|FlClash' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -First 1).IPAddress
    if ($myIp) {
        $subnet = $myIp -replace '\d+$', ''  # e.g. "192.168.15."
        Write-Host "Scanning ${subnet}0/24 for Docker host..."

        # Quick scan: .1 → .20 first, then .21 → .254.
        $candidates = @(1..20) + @(21..254)
        foreach ($suffix in $candidates) {
            $addr = "$subnet$suffix"
            if ($addr -eq $myIp) { continue }

            # Fast TCP port check with 500ms timeout (no ping).
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $conn = $tcp.BeginConnect($addr, 2375, $null, $null)
                if ($conn.AsyncWaitHandle.WaitOne(500)) {
                    $tcp.EndConnect($conn)
                    $tcp.Close()
                    # TCP open — now confirm it actually speaks Docker.
                    if (Test-DockerHost $addr) {
                        Write-Host "Found Docker host: $addr"
                        $addr | Out-File -FilePath $dockerHostFile -Encoding utf8 -NoNewline
                        return $addr
                    }
                } else {
                    $tcp.Close()
                }
            }
            catch {
                # Port closed or not reachable — next.
            }
        }
    }

    return $null
}

# ── Main ──────────────────────────────────────────────────────────────────────

try {
    # Ensure Docker CLI is in PATH this session.
    $env:PATH = "$env:PATH;$dockerCliDir"

    $dockerHost = Find-DockerHost
    if (-not $dockerHost) {
        Show-LauncherError @"
Cannot find Docker Engine on the local network.

Please make sure:
1. The host PC (with Docker Desktop) is running.
2. Docker Desktop has "Expose daemon on tcp://localhost:2375" enabled.
3. Both machines are on the same network.

You can manually set the address by creating:
  $dockerHostFile
with a single line like: 192.168.x.x
"@
        exit 1
    }

    $env:DOCKER_HOST = "tcp://${dockerHost}:2375"
    $adminUrl = "http://${dockerHost}:8000/admin/login"

    # Already running? Just open browser.
    if (Test-Grok2API $dockerHost) {
        Start-Process -FilePath $adminUrl
        exit 0
    }

    # Start the anti-ban Docker stack.
    Write-Host "Starting Grok2API on Docker host ${dockerHost}..."
    Set-Location $projectRoot

    $result = & $dockerCli compose -f docker-compose.warp.yml up -d 2>&1
    if ($LASTEXITCODE -ne 0) {
        Show-LauncherError "Failed to start Docker services:`n$result"
        exit 1
    }

    # Wait for grok2api to become healthy.
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Seconds 1

        if (Test-Grok2API $dockerHost) {
            Start-Process -FilePath $adminUrl
            exit 0
        }

        $status = & $dockerCli ps --filter "name=grok2api" --format "{{.Status}}" 2>&1
        if ($status -match 'Restarting|Exited') {
            $logs = & $dockerCli logs grok2api --tail=15 2>&1
            Show-LauncherError "grok2api container failed to start:`n`n$logs"
            exit 1
        }
    }

    Show-LauncherError "grok2api did not become ready within 120 seconds."
    exit 1
}
catch {
    Show-LauncherError $_.Exception.Message
    exit 1
}
