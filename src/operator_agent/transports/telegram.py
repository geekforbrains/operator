"""Telegram bot transport."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from ..core import Runtime
from ..providers import PROVIDER_NAMES

log = logging.getLogger(__name__)


def _restart() -> None:
    """Restart the operator service (platform-aware) or re-exec the process."""
    if sys.platform == "darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/com.operator.agent.plist")
        if os.path.exists(plist):
            uid = str(os.getuid())
            os.execvp(
                "launchctl",
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.operator.agent"],
            )
    elif sys.platform == "linux":
        os.execvp("systemctl", ["systemctl", "--user", "restart", "operator.service"])
    # Fallback: re-exec ourselves
    os.execvp(sys.executable, [sys.executable, "-m", "operator_agent.cli", "serve"])


class TelegramContext:
    """TransportContext implementation for Telegram."""

    def __init__(self, update: Update):
        self._update = update

    async def reply(self, text: str) -> None:
        await self._update.message.reply_text(text)

    async def reply_status(self, text: str):
        return await self._update.message.reply_text(text)

    async def edit_status(self, handle, text: str) -> None:
        await handle.edit_text(text)

    async def delete_status(self, handle) -> None:
        with contextlib.suppress(Exception):
            await handle.delete()


class TelegramTransport:
    """Telegram bot transport."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        tg_cfg = runtime.config.get("telegram", {})
        self.bot_token: str = tg_cfg.get("bot_token", "")
        self.allowed_user_ids: set[int] = set(tg_cfg.get("allowed_user_ids", []))

    def start(self):
        """Start polling for Telegram messages (blocking)."""
        log.info("Starting Telegram transport")
        provider_paths = {
            name: self.runtime._get_provider_path(name)
            for name in PROVIDER_NAMES
        }
        log.info("  providers: %s", provider_paths)

        app = (
            Application.builder()
            .token(self.bot_token)
            .concurrent_updates(True)
            .build()
        )
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _handle_message(self, update: Update, _context):
        """Handle incoming Telegram messages."""
        user = update.effective_user
        if user is None or update.message is None:
            return

        user_id = user.id
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            log.warning("Unauthorized user: %s", user_id)
            return

        rt = self.runtime

        # --- Command dispatch ---
        if text == "!stop":
            await self._handle_stop(update, chat_id)
            return

        provider_shortcuts = {f"!{n}" for n in PROVIDER_NAMES}
        if text.startswith("!use") or text in provider_shortcuts:
            if text in provider_shortcuts:
                text = f"!use {text[1:]}"
            await self._handle_use(update, text, chat_id)
            return

        if text == "!status":
            provider = rt.get_active_provider(chat_id)
            model = rt.get_active_model(chat_id, provider)
            await update.message.reply_text(f"Provider: {provider}\nModel: {model}")
            return

        if text in ("!clear", "!clear all"):
            await self._handle_clear(update, text, chat_id)
            return

        if text == "!models":
            await self._handle_models(update, chat_id)
            return

        if text.startswith("!model"):
            await self._handle_model(update, text, chat_id)
            return

        if text == "!help":
            await update.message.reply_text(
                "!status - Show active provider & model\n"
                "!use claude|codex|gemini - Switch provider\n"
                "!claude|!codex|!gemini - Shortcuts for !use\n"
                "!models - List models for current provider\n"
                "!model <index|name> - Switch model\n"
                "!stop - Kill running process\n"
                "!clear - Clear current provider session\n"
                "!clear all - Clear all provider sessions\n"
                "!restart - Restart the bot\n"
                "!help - Show this message"
            )
            return

        if text == "!restart":
            await update.message.reply_text("Restarting...")
            asyncio.get_event_loop().call_later(1, _restart)
            return

        if text.startswith("!"):
            await update.message.reply_text("Unknown command. Try !help")
            return

        # --- Regular message → dispatch to provider ---
        provider = rt.get_active_provider(chat_id)
        lock = rt.get_chat_lock(chat_id)

        if lock.locked():
            log.info(
                "Rejected message (lock held) chat_id=%s provider=%s: %.50s",
                chat_id,
                provider,
                text,
            )
            await update.message.reply_text(
                "A request is already running. Use !stop to cancel it."
            )
            return

        log.info(
            "Processing message chat_id=%s provider=%s: %.80s",
            chat_id,
            provider,
            text,
        )

        ctx = TelegramContext(update)
        async with lock:
            task = asyncio.create_task(
                rt.process_request(provider, text, chat_id, ctx)
            )
            rt.running_task_by_chat[chat_id] = task
            try:
                await task
            except asyncio.CancelledError:
                log.info("Task cancelled by user for chat_id=%s", chat_id)
            finally:
                current = rt.running_task_by_chat.get(chat_id)
                if current is task:
                    rt.running_task_by_chat.pop(chat_id, None)

    # --- Command handlers ---

    async def _handle_stop(self, update: Update, chat_id: int):
        log.info("Stop requested for chat_id=%s", chat_id)
        had_something, error = await self.runtime.stop_chat(chat_id)
        if not had_something:
            await update.message.reply_text("No process running.")
        elif error:
            await update.message.reply_text(f"Error stopping: {error}")
        else:
            await update.message.reply_text("Process stopped.")
            log.info("Stopped process for chat_id=%s", chat_id)

    async def _handle_use(self, update: Update, text: str, chat_id: int):
        parts = text.split()
        if len(parts) != 2 or parts[1] not in PROVIDER_NAMES:
            names = "|".join(PROVIDER_NAMES)
            await update.message.reply_text(f"Usage: !use {names}")
            return

        rt = self.runtime
        old_provider = rt.get_active_provider(chat_id)
        provider = parts[1]
        rt.active_provider_by_chat[chat_id] = provider
        rt.save_state()
        log.info(
            "Provider switched: %s -> %s (chat_id=%s)", old_provider, provider, chat_id
        )
        await update.message.reply_text(f"Provider set to {provider}.")

    async def _handle_models(self, update: Update, chat_id: int):
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])
        if not models:
            await update.message.reply_text(
                f"No models configured for {provider}."
            )
            return

        active = rt.get_active_model(chat_id, provider)
        lines = [f"Models for {provider}:"]
        for i, m in enumerate(models, 1):
            marker = " (active)" if m == active else ""
            lines.append(f"  {i}. {m}{marker}")
        await update.message.reply_text("\n".join(lines))

    async def _handle_model(self, update: Update, text: str, chat_id: int):
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])
        parts = text.split(maxsplit=1)

        if len(parts) != 2 or not parts[1].strip():
            await update.message.reply_text(
                "Usage: !model <index|name>\nUse !models to see options."
            )
            return

        choice = parts[1].strip()

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                selected = models[idx]
            else:
                await update.message.reply_text(
                    f"Invalid index. Use 1-{len(models)}.\nUse !models to see options."
                )
                return
        elif choice in models:
            selected = choice
        else:
            await update.message.reply_text(
                f"Unknown model '{choice}'.\nUse !models to see options."
            )
            return

        old_model = rt.get_active_model(chat_id, provider)
        rt.active_model_by_chat_provider[(chat_id, provider)] = selected
        rt.save_state()
        log.info(
            "Model switched: %s %s -> %s (chat_id=%s)",
            provider,
            old_model,
            selected,
            chat_id,
        )
        await update.message.reply_text(f"{provider} model set to {selected}.")

    async def _handle_clear(self, update: Update, text: str, chat_id: int):
        rt = self.runtime
        clear_all = text.strip() == "!clear all"

        if clear_all:
            log.info(
                "Clearing all sessions for all providers (chat_id=%s)", chat_id
            )
        else:
            active = rt.get_active_provider(chat_id)
            log.info("Clearing session for %s (chat_id=%s)", active, chat_id)

        parts = []
        for prov_name in PROVIDER_NAMES:
            if clear_all or rt.get_active_provider(chat_id) == prov_name:
                sid = rt.session_by_chat_provider.pop((chat_id, prov_name), None)
                provider = rt.make_provider(prov_name)
                summary = provider.clear_session(sid, rt.working_dir)
                parts.append(f"{prov_name.capitalize()}: {summary}")

        rt.save_state()
        msg = "Cleared all providers!" if clear_all else "Cleared current provider!"
        await update.message.reply_text(f"{msg}\n" + "\n".join(parts))
