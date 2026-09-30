# Run on the Windows client. Windows OpenSSH must already be installed.
[CmdletBinding()]
param(
    [Alias('SshHost')][string]$HostName = 'cachyos',
    [string]$SshUser = 'cachyos',
    [ValidateRange(1, 65535)][int]$LocalPort = 8788,
    [string]$RemoteDir = '/home/cachyos/ai-scientist',
    [switch]$NoStartLab,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
if ($HostName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*$' -or
    $SshUser -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9._-]*$') {
    throw 'Invalid SSH host or user; use a hostname or SSH config alias.'
}
if ($RemoteDir -notmatch '^/[a-zA-Z0-9._/-]+$') {
    throw 'RemoteDir must be an absolute path using letters, digits, /, _, . and -.'
}
$ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
if (-not $ssh) { throw 'Windows OpenSSH ssh.exe is required.' }

# Probe by binding: never browse a server that already owns the chosen port.
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $LocalPort)
$probe.Server.ExclusiveAddressUse = $true
try { $probe.Start() }
catch { throw "Local port $LocalPort is occupied; choose -LocalPort." }
finally { $probe.Stop() }

$sshOptions = @('-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
if (-not $NoStartLab) {
    # RemoteDir is restricted above; single quotes are preserved by SSH's shell command.
    & $ssh @sshOptions -l $SshUser $HostName "cd '$RemoteDir' && bash ops/start-lab.sh"
    if ($LASTEXITCODE -ne 0) { throw "Remote Lab launcher failed ($LASTEXITCODE)." }
}

$tunnel = $null
try {
    # All arguments here are validated and contain no whitespace or native quoting chars.
    $arguments = $sshOptions + @('-N', '-T', '-L', "127.0.0.1:${LocalPort}:127.0.0.1:8788",
        '-l', $SshUser, $HostName)
    $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -NoNewWindow -PassThru
    $url = "http://127.0.0.1:$LocalPort/"
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($tunnel.HasExited) { throw 'SSH tunnel exited before readiness.' }
        $response = $null
        try {
            $request = [System.Net.HttpWebRequest]::Create($url)
            $request.Proxy = $null
            $request.Timeout = 1000
            $request.ReadWriteTimeout = 1000
            $request.AllowAutoRedirect = $false
            $response = $request.GetResponse()
            $ready = ([int]$response.StatusCode -eq 200)
        }
        catch { $ready = $false }
        finally { if ($null -ne $response) { $response.Close() } }
        if ($ready) {
            Start-Sleep -Milliseconds 200
            if ($tunnel.HasExited) { throw 'SSH tunnel exited before readiness.' }
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'Lab did not respond within 60 seconds.' }
    Write-Host "Lab: $url"
    Write-Host 'Keep this terminal open. Ctrl+C closes only this SSH tunnel.'
    if (-not $NoBrowser) { Start-Process $url }
    # Short waits keep Ctrl+C responsive; finally cleans up the exact child we own.
    while (-not $tunnel.HasExited) { Start-Sleep -Milliseconds 250 }
    if ($tunnel.ExitCode -ne 0) { throw "SSH tunnel exited ($($tunnel.ExitCode))." }
}
finally {
    if ($null -ne $tunnel) {
        if (-not $tunnel.HasExited) { $tunnel.Kill(); $tunnel.WaitForExit() }
        $tunnel.Dispose()
    }
}
