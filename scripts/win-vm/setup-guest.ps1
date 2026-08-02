# Guest provisioning for the Blurt Windows test VM.
#
# Runs in two phases, for a reason worth understanding before editing:
#
#   1. specialize (no arguments) - everything that works offline. Runs as SYSTEM
#      from autounattend.xml, which is the only context with a full admin token:
#      Windows 11 runs FirstLogonCommands with a UAC-filtered one.
#
#   2. -EnsureSshd - installing the OpenSSH server, via a scheduled task that
#      fires at startup. This CANNOT go in specialize. That pass runs before
#      Windows Update is properly up, so Add-WindowsCapability blocks forever on
#      a Feature-on-Demand download and Windows Setup hangs with a DISM progress
#      bar that never moves.
#
# Nothing here may exit non-zero during specialize: Windows Setup treats that as
# fatal and aborts the install unrecoverably. Everything is best-effort and both
# phases end in `exit 0`.

param([switch]$EnsureSshd)

$ErrorActionPreference = 'SilentlyContinue'
$log = 'C:\Windows\Temp\blurt-setup.log'
$taskName = 'BlurtEnsureSshd'
$selfPath = 'C:\Windows\Setup\Scripts\setup-guest.ps1'

function Note($msg) {
    "$(Get-Date -Format o)  $msg" | Out-File -FilePath $log -Append -Encoding utf8
}

# Create a registry key only if it is missing.
#
# Do NOT reach for `New-Item -Force` here. In the registry provider that does not
# mean "ensure it exists" - it DELETES AND RECREATES the key, taking every
# subkey and value with it. Doing that to the OOBE key wipes the plugin
# registrations and the install then dies with "Windows could not complete the
# installation", logging `Failed to open OOBE plugin key [hr=0x80070002]`.
function Ensure-Key($path) {
    if (-not (Test-Path $path)) {
        New-Item -Path $path -Force | Out-Null
    }
}

# ---------------------------------------------------------------------------
# Phase 2: install the OpenSSH server, once the machine is properly booted.
# ---------------------------------------------------------------------------
if ($EnsureSshd) {
    Note 'ensure-sshd task started'

    for ($i = 1; $i -le 30; $i++) {
        if ((Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State -eq 'Installed') {
            Note "OpenSSH capability present (check $i)"
            break
        }
        Note "installing OpenSSH capability, attempt $i"
        try {
            Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
        } catch {
            Note "attempt $i failed: $_"
        }
        Start-Sleep -Seconds 20
    }

    try {
        Set-Service sshd -StartupType Automatic
        Start-Service sshd
        Note "sshd status: $((Get-Service sshd).Status)"
    } catch {
        Note "could not start sshd: $_"
    }

    # Done for good once the service is actually running - otherwise leave the
    # task in place so the next boot tries again.
    if ((Get-Service sshd).Status -eq 'Running') {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Note 'unregistered the ensure-sshd task'
        } catch {
            Note "could not unregister the task: $_"
        }
    }

    Note 'ensure-sshd task finished'
    exit 0
}

# ---------------------------------------------------------------------------
# Phase 1: specialize. Offline work only.
# ---------------------------------------------------------------------------
Note 'starting guest setup (specialize)'

# Belt and braces for the 24H2+ "let's connect you to a network" gate. The
# LocalAccount in the answer file is normally enough to skip the
# Microsoft-account flow; this is the documented fallback and is harmless on
# builds that ignore it.
try {
    $oobeKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE'
    Ensure-Key $oobeKey
    Set-ItemProperty -Path $oobeKey -Name BypassNRO -Value 1 -Type DWord
    Note 'set BypassNRO'
} catch {
    Note "could not set BypassNRO: $_"
}

# Installing the OpenSSH capability does NOT open the firewall. Without this the
# service runs while port 22 stays shut, which from the host is indistinguishable
# from a failed install. The rule can be created before the feature exists.
try {
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    Note 'opened the firewall for port 22'
} catch {
    Note "could not add the SSH firewall rule: $_"
}

try {
    New-NetFirewallRule -Name icmpv4-in -DisplayName 'ICMPv4 in' `
        -Enabled True -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow | Out-Null
} catch {
    Note "could not add the ICMP rule: $_"
}

# PowerShell rather than cmd.exe for ssh sessions.
try {
    Ensure-Key 'HKLM:\SOFTWARE\OpenSSH'
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
        -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Type String
} catch {
    Note "could not set the default ssh shell: $_"
}

# win-vm.sh substitutes the host's public key when it builds the ISO.
# Administrators authenticate through administrators_authorized_keys, not the
# per-user file, and sshd ignores that file unless its ACL is locked down to
# SYSTEM + Administrators.
$pubkey = '@@AUTHORIZED_KEY@@'
if ($pubkey -and -not $pubkey.StartsWith('@@')) {
    try {
        $dir = 'C:\ProgramData\ssh'
        $file = Join-Path $dir 'administrators_authorized_keys'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Set-Content -Path $file -Value $pubkey -Encoding ascii
        icacls $file /inheritance:r /grant 'SYSTEM:F' /grant 'BUILTIN\Administrators:F' | Out-Null
        Note 'installed the host public key'
    } catch {
        Note "could not install the authorized key: $_"
    }
} else {
    Note 'no public key was substituted into this script'
}

# Hand the network-dependent half off to a startup task. The answer ISO may not
# be attached (or may move drive letter) by then, so copy the script to C: first.
try {
    New-Item -ItemType Directory -Force -Path (Split-Path $selfPath) | Out-Null
    Copy-Item -Path $PSCommandPath -Destination $selfPath -Force

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $selfPath -EnsureSshd"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Note 'registered the ensure-sshd startup task'
} catch {
    Note "could not register the ensure-sshd task: $_"
}

# If the ScheduledTasks module was not usable this early, fall back to
# schtasks.exe. Losing this task means losing SSH entirely, so it is worth the
# belt and braces.
if (-not (Get-ScheduledTask -TaskName $taskName)) {
    try {
        $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File $selfPath -EnsureSshd"
        schtasks /create /tn $taskName /tr $cmd /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
        Note 'registered the ensure-sshd task via schtasks'
    } catch {
        Note "schtasks fallback also failed: $_"
    }
}

Note 'guest setup finished (specialize)'

# Never let a failure here abort Windows Setup.
exit 0
