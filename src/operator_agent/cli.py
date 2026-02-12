"""CLI entry point for Operator."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .config import CONFIG_FILE, detect_providers, load_config, save_config

log = logging.getLogger(__name__)

app = typer.Typer(help="Operator - Personal AI Agent", no_args_is_help=True)
console = Console()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "telegram",
    "telegram.ext",
    "telegram.ext._application",
    "telegram.ext._updater",
    "telegram.ext._base_update_handler",
    "telegram._bot",
    "hpack",
    "urllib3",
    "h11",
    "h2",
)


# --- Telegram API helpers (stdlib only, no extra deps) ---


def _tg_call(token: str, method: str, **params) -> dict:
    """Call a Telegram Bot API method. Returns the parsed JSON response."""
    url = TELEGRAM_API.format(token=token, method=method)
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _tg_validate_token(token: str) -> dict | None:
    """Validate a bot token via getMe. Returns bot info or None."""
    try:
        result = _tg_call(token, "getMe")
        if result.get("ok"):
            return result["result"]
    except Exception:
        log.debug("Token validation failed", exc_info=True)
    return None


def _tg_clear_updates(token: str) -> None:
    """Clear any pending updates so we only capture fresh messages."""
    try:
        result = _tg_call(token, "getUpdates", offset=-1, timeout=0)
        if result.get("ok") and result.get("result"):
            last_id = result["result"][-1]["update_id"]
            _tg_call(token, "getUpdates", offset=last_id + 1, timeout=0)
    except Exception:
        log.debug("Failed to clear updates", exc_info=True)


def _tg_wait_for_message(token: str, timeout: int = 120) -> dict | None:
    """Poll for the first message. Returns user/chat info or None on timeout."""
    _tg_clear_updates(token)

    start = time.time()
    last_update_id = 0
    while time.time() - start < timeout:
        try:
            params = {"timeout": 5}
            if last_update_id:
                params["offset"] = last_update_id + 1
            result = _tg_call(token, "getUpdates", **params)
            if result.get("ok"):
                for update in result.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message")
                    if msg and msg.get("from"):
                        _tg_call(
                            token,
                            "getUpdates",
                            offset=last_update_id + 1,
                            timeout=0,
                        )
                        user = msg["from"]
                        return {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "first_name": user.get("first_name"),
                            "chat_id": msg["chat"]["id"],
                        }
        except Exception:
            log.debug("Polling error", exc_info=True)
        time.sleep(1)
    return None


def _tg_send_message(token: str, chat_id: int, text: str) -> bool:
    """Send a message. Returns True on success."""
    try:
        result = _tg_call(token, "sendMessage", chat_id=chat_id, text=text)
        return result.get("ok", False)
    except Exception:
        return False


# --- Service installation ---


def _find_operator_bin() -> str | None:
    """Locate the operator binary."""
    operator_bin = shutil.which("operator")
    if not operator_bin:
        venv_bin = os.path.join(sys.prefix, "bin", "operator")
        if os.path.exists(venv_bin):
            operator_bin = venv_bin
    return operator_bin


def _build_path_str(operator_bin: str) -> str:
    """Build a PATH string from detected CLI locations."""
    path_dirs: set[str] = set()
    path_dirs.add(os.path.dirname(operator_bin))
    for cmd in ("claude", "codex", "gemini", "node", "npx"):
        p = shutil.which(cmd)
        if p:
            path_dirs.add(os.path.dirname(p))
    path_dirs.update(["/usr/local/bin", "/usr/bin", "/bin"])
    return ":".join(sorted(path_dirs))


def _setup_service(config: dict) -> str | None:
    """Dispatch to platform-specific service installer. Returns platform name if installed."""
    if sys.platform == "linux":
        return "systemd" if _setup_systemd(config) else None
    elif sys.platform == "darwin":
        return "launchd" if _setup_launchd(config) else None
    else:
        console.print(
            f"[yellow]Automatic service install not supported on {sys.platform}[/]"
        )
        console.print("Run [bold]operator serve[/] manually to start.")
        return None


def _setup_systemd(config: dict) -> bool:
    """Offer to install a systemd user service. Returns True if installed."""
    service_dir = os.path.expanduser("~/.config/systemd/user")
    service_path = os.path.join(service_dir, "operator.service")

    if os.path.exists(service_path):
        console.print("  systemd service already exists.")
        if not Confirm.ask("  Update it?", default=False):
            return False
    else:
        if not Confirm.ask("  Install systemd service to run on boot?", default=True):
            return False

    operator_bin = _find_operator_bin()
    if not operator_bin:
        console.print("[yellow]  Could not find 'operator' binary. Skipping service install.[/]")
        return False

    path_str = _build_path_str(operator_bin)
    working_dir = config.get("working_dir", os.getcwd())

    unit = f"""\
[Unit]
Description=Operator - Personal AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={operator_bin} serve
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PATH={path_str}

[Install]
WantedBy=default.target
"""

    os.makedirs(service_dir, exist_ok=True)
    with open(service_path, "w") as f:
        f.write(unit)

    console.print(f"  [green]\u2713[/] Service written to {service_path}")

    try:
        xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env = {
            **os.environ,
            "XDG_RUNTIME_DIR": xdg,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg}/bus",
        }
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            env=env,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "operator.service"],
            env=env,
            capture_output=True,
        )
        console.print("  [green]\u2713[/] Service enabled and started.")
    except Exception:
        console.print("  Service file written. Start it with:")
        console.print("    systemctl --user daemon-reload")
        console.print("    systemctl --user enable --now operator.service")

    return True


def _setup_launchd(config: dict) -> bool:
    """Offer to install a macOS launchd user agent. Returns True if installed."""
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(plist_dir, "com.operator.agent.plist")

    if os.path.exists(plist_path):
        console.print("  launchd agent already exists.")
        if not Confirm.ask("  Update it?", default=False):
            return False
    else:
        if not Confirm.ask("  Install launchd agent to run on login?", default=True):
            return False

    operator_bin = _find_operator_bin()
    if not operator_bin:
        console.print("[yellow]  Could not find 'operator' binary. Skipping service install.[/]")
        return False

    path_str = _build_path_str(operator_bin)
    working_dir = config.get("working_dir", os.getcwd())
    log_path = os.path.expanduser("~/.operator/operator.log")

    plist = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.operator.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>{operator_bin}</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_str}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""

    os.makedirs(plist_dir, exist_ok=True)

    if os.path.exists(plist_path):
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)

    with open(plist_path, "w") as f:
        f.write(plist)

    console.print(f"  [green]\u2713[/] Plist written to {plist_path}")

    try:
        subprocess.run(["launchctl", "load", plist_path], capture_output=True, check=True)
        console.print("  [green]\u2713[/] Agent loaded and started.")
    except Exception:
        console.print("  Plist written. Load it with:")
        console.print(f"    launchctl load {plist_path}")

    return True


def _check_service_running(service: str) -> None:
    """Check if the installed service is running and report status."""
    try:
        if service == "launchd":
            result = subprocess.run(
                ["launchctl", "list", "com.operator.agent"],
                capture_output=True,
            )
            running = result.returncode == 0
        elif service == "systemd":
            xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            env = {
                **os.environ,
                "XDG_RUNTIME_DIR": xdg,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg}/bus",
            }
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "operator.service"],
                env=env,
                capture_output=True,
            )
            running = result.returncode == 0
        else:
            return

        if running:
            console.print("[green]\u2713[/] Service is running.")
        else:
            console.print("[yellow]! Service is not running.[/]")
    except Exception:
        log.debug("Service status check failed", exc_info=True)


# --- Setup wizard steps ---


def _setup_providers():
    """Step 1: detect and display available provider CLIs."""
    console.rule("[bold]Step 1 \u00b7 Provider Detection")

    available = detect_providers()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("status", width=3)
    table.add_column("name")
    for name, found in available.items():
        if found:
            table.add_row("[green]\u2713[/]", name)
        else:
            table.add_row("[red]\u2717[/]", f"[dim]{name}[/]")
    console.print(table)

    if not any(available.values()):
        console.print()
        console.print("[yellow]No provider CLIs found on PATH.[/]")
        console.print("Install at least one of: claude, codex, gemini")
        console.print("[dim]You can continue setup and install providers later.[/]")


def _setup_telegram(config: dict) -> int | None:
    """Step 2: configure Telegram bot and capture user. Returns chat_id or None."""
    console.rule("[bold]Step 2 \u00b7 Telegram Bot Setup")

    current_token = config.get("telegram", {}).get("bot_token", "")
    current_users = config.get("telegram", {}).get("allowed_user_ids", [])
    bot_info = None

    # Check existing config
    if current_token:
        bot_info = _tg_validate_token(current_token)
        if bot_info:
            bot_name = bot_info.get("username", "unknown")
            console.print(f"  Current bot: [bold]@{bot_name}[/]")
            if current_users:
                console.print(f"  Allowed users: {current_users}")
            if not Confirm.ask("\n  Reconfigure Telegram?", default=False):
                console.print("  Keeping current Telegram config.")
                return None
            current_users = []
        else:
            console.print("[yellow]  Existing token is invalid, let's set up a new one.[/]")
        current_token = ""

    # Prompt for new token
    console.print("  To connect Operator to Telegram, you need a bot token.\n")
    console.print("  If you don't have one yet:")
    console.print("    1. Open Telegram and message @BotFather")
    console.print("    2. Send /newbot")
    console.print("    3. Choose a name and username for your bot")
    console.print("    4. BotFather will send you a token\n")

    while True:
        token = Prompt.ask("  Bot token")
        if not token:
            console.print("[red]  Token is required.[/]")
            continue

        with console.status("Validating token..."):
            bot_info = _tg_validate_token(token)

        if bot_info:
            bot_name = bot_info.get("username", "unknown")
            console.print(f"  [green]\u2713[/] Connected! Bot: [bold]@{bot_name}[/]")
            config.setdefault("telegram", {})["bot_token"] = token
            current_token = token
            break

        console.print("[red]  \u2717 Could not connect. Check the token and try again.[/]")

    # Auto-capture user ID
    if current_users:
        return None

    bot_name = bot_info.get("username", "unknown")
    console.print("\n  Now let's link your Telegram account.")
    console.print(f"  Send any message to [bold]@{bot_name}[/] in Telegram.\n")

    with console.status("Waiting for message..."):
        user_info = _tg_wait_for_message(current_token, timeout=120)

    if not (user_info and user_info.get("user_id")):
        console.print("[yellow]  Timed out. No message received.[/]")
        console.print("  You can add your user ID manually.")
        console.print("  Edit ~/.operator/config.json \u2192 telegram.allowed_user_ids")
        return None

    user_id = user_info["user_id"]
    name = user_info.get("first_name") or user_info.get("username") or "?"
    console.print(f"  [green]\u2713[/] Got it! {name} (ID: {user_id})")
    config.setdefault("telegram", {})["allowed_user_ids"] = [user_id]
    return user_info.get("chat_id")


# --- Commands ---


@app.command()
def setup():
    """Interactive setup wizard."""
    console.print(Panel("Operator Setup", subtitle=f"v{__version__}", expand=False))
    config = load_config()

    _setup_providers()
    captured_chat_id = _setup_telegram(config)

    # Step 3: Working directory
    console.rule("[bold]Step 3 \u00b7 Working Directory")
    console.print("  This is where Operator runs agent commands from.")
    console.print("  Agents read and create files here to solve tasks.\n")
    current_dir = config.get("working_dir", os.getcwd())
    new_dir = Prompt.ask("  Working directory", default=current_dir)
    if new_dir != current_dir:
        config["working_dir"] = os.path.abspath(new_dir)

    # Save config
    with console.status("Saving config..."):
        save_config(config)
        os.chmod(CONFIG_FILE, 0o600)
    console.print(f"[green]\u2713[/] Config saved to {CONFIG_FILE}")

    # Send test message
    token = config.get("telegram", {}).get("bot_token", "")
    users = config.get("telegram", {}).get("allowed_user_ids", [])
    chat_id = captured_chat_id or (users[0] if users else None)
    if token and chat_id:
        with console.status("Sending test message..."):
            sent = _tg_send_message(
                token, chat_id,
                f"Operator v{__version__} setup complete. Ready to go.",
            )
        if sent:
            console.print("[green]\u2713[/] Test message sent! Check Telegram.")
        else:
            console.print(
                "[yellow]Failed to send test message. You can test later with 'operator serve'.[/]"
            )

    # Step 4: Background service
    console.rule("[bold]Step 4 \u00b7 Background Service (optional)")
    service = _setup_service(config)

    # Summary
    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("key", style="bold")
    summary.add_column("value")
    summary.add_row("Config", str(CONFIG_FILE))

    if service == "launchd":
        _check_service_running(service)
        log_path = "~/.operator/operator.log"
        summary.add_row("Logs", f"tail -f {log_path}")
        summary.add_row("Stop", "launchctl unload ~/Library/LaunchAgents/com.operator.agent.plist")
        summary.add_row("Start", "launchctl load ~/Library/LaunchAgents/com.operator.agent.plist")
    elif service == "systemd":
        _check_service_running(service)
        summary.add_row("Logs", "journalctl --user -u operator -f")
        summary.add_row("Stop", "systemctl --user stop operator")
        summary.add_row("Start", "systemctl --user start operator")
        summary.add_row("Restart", "systemctl --user restart operator")
    else:
        summary.add_row("Run", "operator serve")

    console.print(Panel(summary, title="Setup Complete", border_style="green", expand=False))


@app.command()
def serve(
    transport: Annotated[
        str, typer.Option(help="Transport to use")
    ] = "telegram",
    working_dir: Annotated[
        str | None, typer.Option("--working-dir", help="Override working directory")
    ] = None,
):
    """Start the bot."""
    from .core import Runtime

    config = load_config()

    if working_dir:
        config["working_dir"] = working_dir

    wd = config.get("working_dir", os.getcwd())
    os.chdir(wd)

    runtime = Runtime(config)
    runtime.init_config_dir()
    runtime.load_state()

    log = logging.getLogger("operator_agent")
    log.info("Starting Operator v%s", __version__)
    log.info("  working_dir=%s", wd)

    if transport == "telegram":
        from .transports.telegram import TelegramTransport

        bot_token = config.get("telegram", {}).get("bot_token", "")
        if not bot_token:
            console.print("[red]Error: No Telegram bot token configured.[/]")
            console.print("Run [bold]operator setup[/] to configure.")
            raise typer.Exit(1)

        t = TelegramTransport(runtime)
        t.start()
    else:
        console.print(f"[red]Unknown transport: {transport}[/]")
        raise typer.Exit(1)


# --- App setup ---


def _version_callback(value: bool):
    if value:
        console.print(f"operator {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
):
    """Operator - Personal AI Agent."""
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main():
    app()


if __name__ == "__main__":
    main()
