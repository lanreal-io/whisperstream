# run_dev.ps1 - Start backend, frontend, and Tauri in separate Windows Terminal tabs
# Usage: powershell scripts/run_dev.ps1
#        powershell scripts/run_dev.ps1 -NoTauri        # skip Tauri, just backend + frontend
#        powershell scripts/run_dev.ps1 -NoHotkeys      # pass --no-hotkeys to backend
#        powershell scripts/run_dev.ps1 -Experimental    # enable the experimental surface
#        powershell scripts/run_dev.ps1 -BackendOnly     # open only the Backend tab
#        powershell scripts/run_dev.ps1 -Check           # check prerequisites, then exit without launching

param(
    [switch]$NoTauri,
    [switch]$BackendOnly,
    [switch]$NoHotkeys,
    [switch]$Experimental,
    [switch]$Check
)

$root = (Resolve-Path "$PSScriptRoot\..").Path
$port = 38420
$env:WHISPERSTREAM_AGENT_PLUGIN_VARIANT = "dev"

$hotkeyFlag = if ($NoHotkeys) { " --no-hotkeys" } else { "" }

# Resolve the backend environment on the launcher process. Before opening the
# Backend tab, these values are serialized into its encoded startup command.
# Windows Terminal can reload a tab's environment instead of inheriting this
# process, especially when `wt -w 0` targets an existing window.

# Isolate dev runs from the installed prod app's state (config, logs,
# license, acceptance, models). Without this, dev and prod share
# %LOCALAPPDATA%\Whisperstream\ and saves from either side stomp the
# other.
if (-not $env:WHISPERSTREAM_DATA_DIR) {
    $env:WHISPERSTREAM_DATA_DIR = Join-Path $env:LOCALAPPDATA "Whisperstream-dev"
}
$data = [IO.Path]::GetFullPath($env:WHISPERSTREAM_DATA_DIR)
$installed = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Whisperstream"))
if ($data.TrimEnd("\") -eq $installed -or $data.StartsWith($installed + "\", [StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "Choose WHISPERSTREAM_DATA_DIR outside the installed Whisperstream data directory."
    exit 1
}
$env:WHISPERSTREAM_DATA_DIR = $data

# Experimental surface is gated by WHISPERSTREAM_EXPERIMENTAL=1 on the backend.
# Set it on the launcher process; the encoded Backend command below applies it
# to the child, and the frontend reads it back via /api/experimental/enabled.
# Never set in frozen prod builds; this only affects dev sessions.
if ($Experimental) { $env:WHISPERSTREAM_EXPERIMENTAL = "1" }

# -Check verifies prerequisites without installing, fetching or launching.
if ($Check) {
    $requiredCommands = @("uv", "node", "npm")
    if (-not $NoTauri -and -not $BackendOnly) { $requiredCommands += "cargo" }
    foreach ($command in $requiredCommands) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            Write-Error "Required command is missing: $command"
            exit 1
        }
    }
    if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
        Write-Error "Backend environment missing. Run uv sync --locked --extra dev first."
        exit 1
    }
    $checkPorts = if ($BackendOnly) { @($port) } else { @($port, 1420) }
    foreach ($checkPort in $checkPorts) {
        if (Get-NetTCPConnection -State Listen -LocalPort $checkPort -ErrorAction SilentlyContinue) {
            Write-Error "Port $checkPort is in use. Close that development session yourself before launching."
            exit 1
        }
    }
    if (-not $NoTauri -and -not $BackendOnly -and -not (Test-Path (Join-Path $root "src-tauri\binaries\llama-server-x86_64-pc-windows-msvc.exe"))) {
        Write-Host "The local enhancement sidecar is missing; the launcher fetches it on first launch."
    }
    Write-Host "Development data: $data"
    Write-Host "Preflight passed."
    exit 0
}

$backendCmd = "uv run python -m whisperstream.main --serve --port $port$hotkeyFlag"
$frontendCmd = "npm run dev"
$tauriCmd = "cargo tauri dev"

# `wt -w 0` adds tabs to an existing Windows Terminal process. That process may
# create sessions from a freshly reloaded user environment, which drops the
# process-only values resolved above. Put every launcher-managed backend value
# directly in an encoded PowerShell startup script so the child receives the
# exact configuration without exposing semicolons to wt's action parser.
# A calling wrapper can name further backend variables to carry the same way
# in WHISPERSTREAM_DEV_LAUNCHER_ENV (names separated by semicolons).
$backendEnvironmentNames = [System.Collections.Generic.List[string]]::new()
$backendEnvironmentNames.AddRange([string[]]@(
    "WHISPERSTREAM_DATA_DIR",
    "WHISPERSTREAM_AGENT_PLUGIN_VARIANT",
    "WHISPERSTREAM_EXPERIMENTAL"
))
if ($env:WHISPERSTREAM_DEV_LAUNCHER_ENV) {
    foreach ($name in $env:WHISPERSTREAM_DEV_LAUNCHER_ENV.Split(";")) {
        $name = $name.Trim()
        if ($name -and -not $backendEnvironmentNames.Contains($name)) {
            if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
                Write-Error "Invalid environment variable name in WHISPERSTREAM_DEV_LAUNCHER_ENV: $name"
                exit 1
            }
            $backendEnvironmentNames.Add($name)
        }
    }
}
$backendScriptLines = [System.Collections.Generic.List[string]]::new()
foreach ($name in $backendEnvironmentNames) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    if ($null -eq $value) {
        $backendScriptLines.Add("Remove-Item Env:$name -ErrorAction SilentlyContinue")
    } else {
        $literal = "'" + $value.Replace("'", "''") + "'"
        $backendScriptLines.Add('$env:' + $name + ' = ' + $literal)
    }
}
$backendScriptLines.Add($backendCmd)
$backendScript = $backendScriptLines -join "`n"
$backendEncodedCommand = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($backendScript)
)

# Tauri's externalBin resolves to binaries/whisperstream-engine-x86_64-pc-windows-msvc.exe
# In dev the sidecar isn't spawned (guarded by #[cfg(not(debug_assertions))]) but the file
# must exist for cargo build to succeed. Create an empty stub if missing.
#
# The llama-server sidecar (binaries/llama-server-x86_64-pc-windows-msvc.exe) is
# different: it actually gets spawned at runtime when Local enhancement is on, so
# we fetch the real binary via scripts/fetch_llama_server.py. The script is
# idempotent (cache check + sha256) so re-runs are fast. These artifacts are
# gitignored so a freshly-created worktree starts without them.
if (-not $NoTauri -and -not $BackendOnly) {
    $sidecarStub = Join-Path $root "src-tauri\binaries\whisperstream-engine-x86_64-pc-windows-msvc.exe"
    $sidecarDir = Split-Path $sidecarStub -Parent
    if (-not (Test-Path $sidecarDir)) { New-Item -ItemType Directory -Path $sidecarDir -Force | Out-Null }
    if (-not (Test-Path $sidecarStub)) { New-Item -ItemType File -Path $sidecarStub | Out-Null }

    $llamaServer = Join-Path $root "src-tauri\binaries\llama-server-x86_64-pc-windows-msvc.exe"
    if (-not (Test-Path $llamaServer)) {
        Write-Host "src-tauri/binaries/llama-server-* missing - fetching llama.cpp sidecar (one-time setup)..."
        # Run from the repository root because the fetcher uses relative paths.
        Push-Location $root
        [Environment]::CurrentDirectory = (Get-Location).Path
        try {
            uv run python scripts/fetch_llama_server.py
            if ($LASTEXITCODE -ne 0) {
                Write-Error "fetch_llama_server.py failed with exit code $LASTEXITCODE"
                exit 1
            }
        } finally {
            Pop-Location
            [Environment]::CurrentDirectory = (Get-Location).Path
        }
    }
}

if (-not $BackendOnly) {
    # Both modes end up running `npm run dev` in frontend/ (Tauri via its
    # beforeDevCommand, NoTauri via the Frontend tab). Refresh dependencies when
    # the package manifests change or a direct dependency is missing. The stamp
    # keeps the normal launch path cheap while avoiding stale node_modules installs.
    $frontendDir = Join-Path $root "frontend"
    $frontendNodeModules = Join-Path $frontendDir "node_modules"
    $dependencyStamp = Join-Path $frontendNodeModules ".whisperstream-deps.sha256"
    $dependencyManifests = @(
        Join-Path $frontendDir "package.json"
        Join-Path $frontendDir "package-lock.json"
    )
    $dependencyFingerprint = ($dependencyManifests | ForEach-Object {
        "$(Split-Path $_ -Leaf):$((Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash)"
    }) -join "`n"

    $dependenciesCurrent = (Test-Path -LiteralPath $frontendNodeModules) -and
        (Test-Path -LiteralPath $dependencyStamp) -and
        ((Get-Content -Raw -LiteralPath $dependencyStamp).Trim() -eq $dependencyFingerprint)

    if ($dependenciesCurrent) {
        $packageManifest = Get-Content -Raw -LiteralPath $dependencyManifests[0] | ConvertFrom-Json
        $directDependencies = @($packageManifest.dependencies.PSObject.Properties.Name) +
            @($packageManifest.devDependencies.PSObject.Properties.Name)
        $dependenciesCurrent = -not ($directDependencies | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $frontendNodeModules $_))
        } | Select-Object -First 1)
    }

    if (-not $dependenciesCurrent) {
        Write-Host "Frontend dependencies missing or stale - running 'npm install'..."
        Push-Location $frontendDir
        try {
            npm install
            if ($LASTEXITCODE -ne 0) {
                Write-Error "npm install failed with exit code $LASTEXITCODE"
                exit 1
            }
            $dependencyFingerprint = ($dependencyManifests | ForEach-Object {
                "$(Split-Path $_ -Leaf):$((Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash)"
            }) -join "`n"
            Set-Content -NoNewline -LiteralPath $dependencyStamp -Value $dependencyFingerprint
        } finally {
            Pop-Location
        }
    }
}

if ($BackendOnly) {
    wt -w 0 `
        new-tab -d "$root" --title "Backend" powershell -NoExit -EncodedCommand $backendEncodedCommand
} elseif ($NoTauri) {
    wt -w 0 `
        new-tab -d "$root" --title "Backend" powershell -NoExit -EncodedCommand $backendEncodedCommand `; `
        new-tab -d "$root\frontend" --title "Frontend" powershell -NoExit -Command $frontendCmd
} else {
    wt -w 0 `
        new-tab -d "$root" --title "Backend" powershell -NoExit -EncodedCommand $backendEncodedCommand `; `
        new-tab -d "$root\src-tauri" --title "Tauri" powershell -NoExit -Command $tauriCmd
}
