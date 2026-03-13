from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import typer

from operator_ai.config import LOGS_DIR

service_app = typer.Typer(help="Manage the operator background service.")

_LAUNCHD_LABEL = "ai.operator"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
_SYSTEMD_UNIT = "operator.service"
_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_PATH = _SYSTEMD_DIR / _SYSTEMD_UNIT


def _launchd_domain_target() -> str:
    return f"gui/{os.getuid()}"


def _launchd_service_target() -> str:
    return f"{_launchd_domain_target()}/{_LAUNCHD_LABEL}"


def _launchd_service_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "list", _LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _require_launchd_plist() -> None:
    if not _PLIST_PATH.exists():
        print("Service not installed.")
        raise typer.Exit(code=1)


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _find_operator_bin() -> str:
    """Find the operator executable path."""
    import shutil

    path = shutil.which("operator")
    if path:
        return path
    return str(Path(sys.executable).parent / "operator")


def _generate_plist(bin_path: str) -> str:
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{bin_path}</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{current_path}</string>
            </dict>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{LOGS_DIR / "operator.log"}</string>
            <key>StandardErrorPath</key>
            <string>{LOGS_DIR / "operator.log"}</string>
            <key>WorkingDirectory</key>
            <string>{Path.home()}</string>
        </dict>
        </plist>""")


def _generate_systemd_unit(bin_path: str) -> str:
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    return textwrap.dedent(f"""\
        [Unit]
        Description=Operator local AI agent runtime

        [Service]
        ExecStart={bin_path}
        Environment=PATH={current_path}
        Restart=on-failure
        RestartSec=5
        StandardOutput=append:{LOGS_DIR / "operator.log"}
        StandardError=append:{LOGS_DIR / "operator.log"}
        WorkingDirectory={Path.home()}

        [Install]
        WantedBy=default.target""")


@service_app.command("install")
def service_install() -> None:
    """Generate and load a service definition (launchd/systemd)."""
    bin_path = _find_operator_bin()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if _is_macos():
        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=False)
        _PLIST_PATH.write_text(_generate_plist(bin_path))
        subprocess.run(
            ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
            check=True,
        )
        print(f"Installed and loaded {_PLIST_PATH}")
    else:
        _SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
        _SYSTEMD_PATH.write_text(_generate_systemd_unit(bin_path))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", _SYSTEMD_UNIT], check=True)
        print(f"Installed and enabled {_SYSTEMD_PATH}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unload and remove the service definition."""
    if _is_macos():
        if _PLIST_PATH.exists():
            if _launchd_service_loaded():
                subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=False)
            _PLIST_PATH.unlink()
            print(f"Unloaded and removed {_PLIST_PATH}")
        else:
            print("Service not installed.")
    else:
        subprocess.run(["systemctl", "--user", "disable", _SYSTEMD_UNIT], check=False)
        subprocess.run(["systemctl", "--user", "stop", _SYSTEMD_UNIT], check=False)
        if _SYSTEMD_PATH.exists():
            _SYSTEMD_PATH.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print(f"Removed {_SYSTEMD_PATH}")
        else:
            print("Service not installed.")


@service_app.command("start")
def service_start() -> None:
    """Start the background service."""
    if _is_macos():
        _require_launchd_plist()
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "kickstart", _launchd_service_target()], check=True)
        else:
            subprocess.run(
                ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
                check=True,
            )
    else:
        subprocess.run(["systemctl", "--user", "start", _SYSTEMD_UNIT], check=True)
    print("Service started.")


@service_app.command("stop")
def service_stop() -> None:
    """Stop the background service."""
    if _is_macos():
        if not _launchd_service_loaded():
            print("Service already stopped.")
            return
        subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=True)
    else:
        subprocess.run(["systemctl", "--user", "stop", _SYSTEMD_UNIT], check=True)
    print("Service stopped.")


@service_app.command("restart")
def service_restart() -> None:
    """Restart the background service."""
    if _is_macos():
        _require_launchd_plist()
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "kickstart", "-k", _launchd_service_target()], check=True)
        else:
            subprocess.run(
                ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
                check=True,
            )
    else:
        subprocess.run(["systemctl", "--user", "restart", _SYSTEMD_UNIT], check=True)
    print("Service restarted.")


@service_app.command("status")
def service_status() -> None:
    """Show whether the service is running."""
    if _is_macos():
        result = subprocess.run(
            ["launchctl", "list", _LAUNCHD_LABEL],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Service not loaded.")
            raise typer.Exit(code=1)
        output = result.stdout
        pid_match = re.search(r'"PID"\s*=\s*(\d+)', output)
        exit_match = re.search(r'"LastExitStatus"\s*=\s*(\d+)', output)
        last_exit = exit_match.group(1) if exit_match else "?"
        if pid_match:
            print(f"Running (PID {pid_match.group(1)}, last exit {last_exit})")
        else:
            print(f"Loaded but not running (last exit {last_exit})")
    else:
        result = subprocess.run(
            ["systemctl", "--user", "status", _SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        print(result.stdout.strip())
        if result.returncode != 0:
            raise typer.Exit(code=1)
