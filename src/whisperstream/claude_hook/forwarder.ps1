# forwarder.ps1 — Claude Code hook forwarder for Whisperstream Claude HUD.
#
# PURPOSE
# -------
# This script is installed as a Claude Code Stop/Notification hook. When Claude
# Code fires a hook event it invokes this script with the event JSON on stdin.
# The script forwards the raw body to the running Whisperstream backend via a
# fire-and-forget POST; the backend owns ALL parsing, filtering, and guarding.
#
# CONTRACT
# --------
# - NEVER blocks Claude Code: -TimeoutSec 1, errors are swallowed, exits 0.
# - NEVER parses or inspects the event body (backend owns that).
# - Discovers the backend port from claude-hud-port.json; falls back to 38420.
#
# DATA DIR RESOLUTION
# -------------------
# This script resolves the Whisperstream data directory the same way the Python
# backend does: it honors $env:WHISPERSTREAM_DATA_DIR when set and non-empty,
# otherwise uses the platform default $env:LOCALAPPDATA\Whisperstream.
# Claude Code inherits the environment of the shell that launched it, so if
# WHISPERSTREAM_DATA_DIR was set when Claude Code was started this forwarder
# will automatically read from the same isolated dev data dir.

try {
    # Read ALL of stdin as raw text (the JSON body from Claude Code).
    $body = [Console]::In.ReadToEnd()

    # Resolve the Whisperstream data directory.
    $dataDir = $env:WHISPERSTREAM_DATA_DIR
    if (-not $dataDir -or $dataDir.Trim() -eq '') {
        $dataDir = Join-Path $env:LOCALAPPDATA 'Whisperstream'
    }

    # Attempt to read the port from the discovery file written at backend startup.
    $port = 38420  # fallback default
    $portFile = Join-Path $dataDir 'claude-hud-port.json'
    if (Test-Path $portFile) {
        try {
            $info = Get-Content $portFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($info.port -and [int]$info.port -gt 0) {
                $port = [int]$info.port
            }
        } catch {
            # File unreadable or corrupt — stay on the fallback port.
        }
    }

    # POST the raw body to the backend endpoint. Short timeout so Claude is
    # never blocked. -UseBasicParsing avoids IE engine dependency.
    $url = "http://127.0.0.1:$port/api/claude-event"
    Invoke-WebRequest `
        -Uri $url `
        -Method POST `
        -Body $body `
        -ContentType 'application/json' `
        -TimeoutSec 1 `
        -UseBasicParsing `
        -ErrorAction Stop | Out-Null

} catch {
    # Swallow ALL errors — this hook must never block or fail Claude Code.
}

exit 0
