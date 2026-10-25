<#
.SYNOPSIS
Sets or removes Whisperstream's machine-level Managed Lockdown policy.

.DESCRIPTION
Uses the explicit 64-bit HKLM registry view. The script is idempotent and is
safe to run repeatedly from an elevated EAS, Intune, Group Policy, or local
administrator deployment context. It never writes an HKCU fallback.

.PARAMETER DisableHistory
Also set DisableHistory=1 when enabling, or remove that value when removing
management. Without this switch, the history policy is unchanged.

.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\set-managed-lockdown-policy.ps1 -Action Enable

.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\set-managed-lockdown-policy.ps1 -Action Remove
#>

[CmdletBinding()]
param(
    [ValidateSet("Enable", "Remove")]
    [string] $Action = "Enable",
    [switch] $DisableHistory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdministrator) {
    throw "Managed Lockdown policy requires an elevated administrator or machine/SYSTEM context."
}

$policyPath = "SOFTWARE\Policies\Whisperstream"
$baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    [Microsoft.Win32.RegistryHive]::LocalMachine,
    [Microsoft.Win32.RegistryView]::Registry64
)

try {
    if ($Action -eq "Enable") {
        $policyKey = $baseKey.CreateSubKey("SOFTWARE\Policies\Whisperstream", $true)
        if ($null -eq $policyKey) {
            throw "Could not open the 64-bit Whisperstream machine-policy key for writing."
        }

        try {
            $policyKey.SetValue(
                "ManagedLockdown",
                1,
                [Microsoft.Win32.RegistryValueKind]::DWord
            )
            if ($DisableHistory) {
                $policyKey.SetValue("DisableHistory", 1, [Microsoft.Win32.RegistryValueKind]::DWord)
                if ($policyKey.GetValue("DisableHistory", $null) -ne 1 -or
                    $policyKey.GetValueKind("DisableHistory") -ne [Microsoft.Win32.RegistryValueKind]::DWord) {
                    throw "History policy verification failed after writing."
                }
            }
            $written = $policyKey.GetValue("ManagedLockdown", $null)
            if ($null -eq $written -or [int] $written -ne 1) {
                throw "Managed Lockdown policy verification failed after writing."
            }
        }
        finally {
            $policyKey.Dispose()
        }

        Write-Output "Managed Lockdown is enabled in the 64-bit HKLM policy view."
    }
    else {
        $policyKey = $baseKey.OpenSubKey($policyPath, $true)
        if ($null -ne $policyKey) {
            try {
                if ($DisableHistory) {
                    $policyKey.DeleteValue("DisableHistory", $false)
                    if ($null -ne $policyKey.GetValue("DisableHistory", $null)) {
                        throw "History policy verification failed after removal."
                    }
                }
                $policyKey.DeleteValue("ManagedLockdown", $false)
                if ($null -ne $policyKey.GetValue("ManagedLockdown", $null)) {
                    throw "Managed Lockdown policy verification failed after removal."
                }
            }
            finally {
                $policyKey.Dispose()
            }
        }

        Write-Output "Managed Lockdown is absent from the 64-bit HKLM policy view."
    }
}
finally {
    $baseKey.Dispose()
    $identity.Dispose()
}

exit 0
