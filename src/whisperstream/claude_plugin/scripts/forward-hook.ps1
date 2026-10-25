# Whisperstream Claude Code plugin hook forwarder.
#
# This hook is deliberately fail-open. It reads stdin once, forwards the
# unmodified event to the local decision endpoint, and emits either one valid
# JSON object or {}. No failure is allowed to block Claude Code.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

$RequestTimeoutSeconds = 125
$MaxRequestCharacters = 1048576
$MaxResponseCharacters = 65536
$MaxDiscoveryBytes = 4096
$OutputJson = '{}'
# Carries the wrapper-minted per-session correlation token (claude-managed-
# sessions Task 2.4), present only when Claude was launched via
# `whisperstream claude`: the wrapper mints a random token per run and
# exports it into this process's environment as WHISPERSTREAM_SESSION_TOKEN.
# Mirrors server.py's _MANAGED_SESSION_TOKEN_HEADER.
$SessionTokenHeaderName = 'x-whisperstream-managed-session-token'

function ConvertTo-AsciiOnlyJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Json
    )

    $Builder = New-Object System.Text.StringBuilder
    foreach ($Character in $Json.ToCharArray()) {
        $CodeUnit = [int][char]$Character
        if ($CodeUnit -le 0x7f) {
            [void]$Builder.Append($Character)
        } else {
            # JSON escapes UTF-16 code units. Non-BMP characters therefore
            # become a surrogate pair, which Claude reconstructs when parsing.
            [void]$Builder.Append(('\u{0:x4}' -f $CodeUnit))
        }
    }
    return $Builder.ToString()
}

function Read-Utf8StandardInput {
    # Claude Code writes the hook payload to stdin as UTF-8.
    #
    # Do NOT use [Console]::In.ReadToEnd() here. Under Windows PowerShell 5.1
    # that decodes stdin with [Console]::InputEncoding, which is the console's
    # OEM codepage (IBM437 / cp850 / cp1252 depending on the machine), so every
    # multi-byte character is shredded into mojibake before this script has
    # touched it: U+1F50A arrives as four garbage chars, an em dash as three.
    # The corruption is codepage-dependent, which made it look intermittent.
    #
    # Decoding the raw stream explicitly is the only reliable read, and it is
    # independent of whatever the host console is configured for.
    $Utf8 = New-Object System.Text.UTF8Encoding($false)
    $Stream = [Console]::OpenStandardInput()
    $Reader = New-Object System.IO.StreamReader($Stream, $Utf8)
    try {
        return $Reader.ReadToEnd()
    } finally {
        $Reader.Dispose()
    }
}

# Unattended runs (`claude -p`, SDK automation) have no person to speak to.
# Claude Code marks them in the hook environment; skip before any discovery.
if ($env:CLAUDE_CODE_SESSION_ATTENDED -eq '0') {
    [Console]::Out.WriteLine($OutputJson)
    exit 0
}

try {
    $Body = Read-Utf8StandardInput
    if (
        [string]::IsNullOrWhiteSpace($Body) -or
        $Body.Length -gt $MaxRequestCharacters
    ) {
        throw 'Invalid hook request size'
    }

    $ParsedInput = $Body | ConvertFrom-Json -ErrorAction Stop
    if (
        $null -eq $ParsedInput -or
        $ParsedInput -is [System.Array] -or
        $ParsedInput -is [string] -or
        $ParsedInput -is [ValueType]
    ) {
        throw 'Hook request must be a JSON object'
    }

    # Cached plugins carry their exact app-data target. Never inherit the other
    # channel's directory or fall back to a shared port.
    $TargetFile = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime-target.json'
    $TargetItem = Get-Item -LiteralPath $TargetFile -ErrorAction Stop
    if ($TargetItem.Length -gt $MaxDiscoveryBytes) { throw 'Invalid target size' }
    $Target = Get-Content -LiteralPath $TargetFile -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
    $DataDir = [string]$Target.data_dir
    if ([string]::IsNullOrWhiteSpace($DataDir) -or -not [System.IO.Path]::IsPathRooted($DataDir)) {
        throw 'Invalid runtime target'
    }
    $PortFile = Join-Path $DataDir 'claude-hud-port.json'
    $PortItem = Get-Item -LiteralPath $PortFile -ErrorAction Stop
    if ($PortItem.Length -gt $MaxDiscoveryBytes) { throw 'Invalid discovery size' }
    $PortInfo = Get-Content -LiteralPath $PortFile -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
    $Port = 0
    $HookToken = [string]$PortInfo.token
    if (
        -not [int]::TryParse([string]$PortInfo.port, [ref]$Port) -or
        $Port -lt 1 -or $Port -gt 65535 -or
        $HookToken -cnotmatch '^[a-f0-9]{64}$'
    ) { throw 'Invalid runtime discovery' }

    $RequestHeaders = @{ 'x-whisperstream-hook-token' = $HookToken }
    $SessionToken = $env:WHISPERSTREAM_SESSION_TOKEN
    if (-not [string]::IsNullOrWhiteSpace($SessionToken)) {
        $RequestHeaders[$SessionTokenHeaderName] = $SessionToken
    }

    # An older production app ignores the new POST header. Verify the runtime
    # without sending transcript-class content before trusting the shared port.
    $Verified = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/agent-hook/verify" `
        -Headers $RequestHeaders -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    $Proof = $Verified.Content | ConvertFrom-Json -ErrorAction Stop
    if ($Verified.StatusCode -ne 200 -or $Proof.ok -ne $true) {
        throw 'Runtime verification failed'
    }

    $RequestBytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
    $Response = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$Port/api/claude-hook" `
        -Method POST `
        -Body $RequestBytes `
        -ContentType 'application/json; charset=utf-8' `
        -Headers $RequestHeaders `
        -TimeoutSec $RequestTimeoutSeconds `
        -UseBasicParsing `
        -ErrorAction Stop

    # Windows PowerShell may decode application/json response content using
    # the active ANSI code page when no charset is present. Decode the raw
    # response bytes as strict UTF-8 so the subsequent JSON escapes preserve
    # the exact marker and all decision/reply text.
    $StrictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    $ResponseBytes = $Response.RawContentStream.ToArray()
    $ResponseText = $StrictUtf8.GetString($ResponseBytes)
    if (
        [string]::IsNullOrWhiteSpace($ResponseText) -or
        $ResponseText.Length -gt $MaxResponseCharacters
    ) {
        throw 'Invalid hook response size'
    }

    $ParsedOutput = $ResponseText | ConvertFrom-Json -ErrorAction Stop
    if (
        $null -eq $ParsedOutput -or
        $ParsedOutput -is [System.Array] -or
        $ParsedOutput -is [string] -or
        $ParsedOutput -is [ValueType]
    ) {
        throw 'Hook response must be a JSON object'
    }

    $Serialized = $ParsedOutput | ConvertTo-Json -Compress -Depth 16
    $AsciiSerialized = ConvertTo-AsciiOnlyJson -Json $Serialized
    if (
        [string]::IsNullOrWhiteSpace($AsciiSerialized) -or
        $AsciiSerialized.Length -gt $MaxResponseCharacters
    ) {
        throw 'Invalid serialized hook response'
    }
    $OutputJson = $AsciiSerialized
} catch {
    $OutputJson = '{}'
}

[Console]::Out.WriteLine($OutputJson)
exit 0
