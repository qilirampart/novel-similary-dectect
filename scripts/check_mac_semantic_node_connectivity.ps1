param(
    [string]$TargetHost = "192.168.97.154",
    [string]$User = "a111",
    [int]$SshPort = 22,
    [int]$OllamaPort = 11434,
    [int]$QdrantPort = 6333,
    [switch]$RunSshProbe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Test-TcpPort {
    param(
        [string]$TargetHost,
        [int]$Port
    )

    $result = Test-NetConnection $TargetHost -Port $Port -WarningAction SilentlyContinue
    [PSCustomObject]@{
        Host = $TargetHost
        Port = $Port
        TcpTestSucceeded = $result.TcpTestSucceeded
        PingSucceeded = $result.PingSucceeded
    }
}

function Test-HttpEndpoint {
    param(
        [string]$Url
    )

    try {
        $body = & curl.exe --silent --show-error --connect-timeout 5 $Url
        [PSCustomObject]@{
            Url = $Url
            Success = $true
            Body = $body
        }
    }
    catch {
        [PSCustomObject]@{
            Url = $Url
            Success = $false
            Body = $_.Exception.Message
        }
    }
}

Write-Section "TCP Ports"
$portResults = @(
    Test-TcpPort -TargetHost $TargetHost -Port $SshPort
    Test-TcpPort -TargetHost $TargetHost -Port $OllamaPort
    Test-TcpPort -TargetHost $TargetHost -Port $QdrantPort
)
$portResults | Format-Table -AutoSize

Write-Section "HTTP APIs"
$ollamaUrl = "http://${TargetHost}:${OllamaPort}/api/version"
$qdrantUrl = "http://${TargetHost}:${QdrantPort}"
$httpResults = @(
    Test-HttpEndpoint -Url $ollamaUrl
    Test-HttpEndpoint -Url $qdrantUrl
)

foreach ($item in $httpResults) {
    Write-Host ""
    Write-Host $item.Url -ForegroundColor Yellow
    if ($item.Success) {
        Write-Host $item.Body
    }
    else {
        Write-Host "FAILED: $($item.Body)" -ForegroundColor Red
    }
}

Write-Section "SSH Hint"
Write-Host "First login command:"
Write-Host "ssh $User@$TargetHost"
Write-Host ""
Write-Host "If this is the first connection, accept the host key and then enter the password."

if ($RunSshProbe) {
    Write-Section "SSH Probe"
    Write-Host "Running verbose SSH probe with BatchMode=yes..."
    & ssh -vv -o BatchMode=yes -o ConnectTimeout=5 "$User@$TargetHost" exit
}
