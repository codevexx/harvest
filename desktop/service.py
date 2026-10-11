"""Run Harvest when the PC starts, before anyone signs in (Windows).

This registers a Task Scheduler task named "Harvest" that starts
`Harvest.exe --headless` at boot as the current user, using S4U ("run whether
the user is logged on or not" without storing a password). Registering a boot
task needs administrator rights once; the tray app asks through UAC.

The background copy steps aside while the tray app runs in a signed-in session
(see harvest_tray.run_headless), so only one agent works at a time.
"""
from __future__ import annotations

import os
import subprocess
import sys

TASK_NAME = "Harvest"
NO_WINDOW = 0x08000000


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60)


def _q(s: str) -> str:
    """Quote a string for a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def installed() -> bool:
    if not sys.platform.startswith("win"):
        return False
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], capture_output=True,
                       creationflags=NO_WINDOW)
    return r.returncode == 0


def install(exe: str, config_path: str) -> str | None:
    """Register and start the boot task. Must run elevated. Returns an error or None."""
    args = f'--headless --config "{config_path}"'
    # Ask Windows for the account (MACHINE\user). USERDOMAIN can say WORKGROUP,
    # e.g. in an SSH session, which Task Scheduler cannot map to an account.
    script = f"""
$ErrorActionPreference = 'Stop'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$a = New-ScheduledTaskAction -Execute {_q(exe)} -Argument {_q(args)}
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
     -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances Queue `
     -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $a -Trigger $t -Principal $p -Settings $s `
     -Description 'Runs Harvest tasks while the PC is on, even before anyone signs in.' -Force | Out-Null
Start-ScheduledTask -TaskName {_q(TASK_NAME)}
"""
    r = _ps(script)
    return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["failed"])[-1][:300]


def uninstall() -> str | None:
    r = _ps(f"""
Stop-ScheduledTask -TaskName {_q(TASK_NAME)} -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false -ErrorAction Stop
""")
    return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["failed"])[-1][:300]


def restart() -> None:
    """Restart the background copy so it picks up new settings (if registered)."""
    if installed():
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, creationflags=NO_WINDOW)
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, creationflags=NO_WINDOW)


def queue_run() -> None:
    """Ask Task Scheduler to start a fresh copy once this one exits (after an update)."""
    subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, creationflags=NO_WINDOW)


def run_elevated(exe: str, args: str) -> bool:
    """Run exe with admin rights (shows the UAC prompt) and wait. True if it succeeded."""
    r = _ps(f"$p = Start-Process -FilePath {_q(exe)} -ArgumentList {_q(args)} -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    return r.returncode == 0
