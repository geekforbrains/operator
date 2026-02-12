"""Core runtime: state management, process spawning, and request handling."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import json
import logging
import os
import tempfile
from asyncio.subprocess import Process
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import CONFIG_DIR, STATE_FILE
from .providers import BaseProvider, get_provider

if TYPE_CHECKING:
    from .transports import TransportContext

log = logging.getLogger(__name__)


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Split text at line boundaries for message size limits."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            remainder = line
            while len(remainder) > limit:
                chunks.append(remainder[:limit])
                remainder = remainder[limit:]
            current = remainder
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


class Runtime:
    """Central runtime holding all state and process management."""

    def __init__(self, config: dict):
        self.config = config
        self.working_dir: str = config.get("working_dir", str(Path.cwd()))
        self.models: dict[str, list[str]] = {
            name: pcfg.get("models", [])
            for name, pcfg in config.get("providers", {}).items()
        }
        self.active_provider_by_chat: dict[int, str] = {}
        self.active_model_by_chat_provider: dict[tuple[int, str], str] = {}
        self.session_by_chat_provider: dict[tuple[int, str], str] = {}
        self.running_process_by_chat: dict[int, Process] = {}
        self.running_task_by_chat: dict[int, asyncio.Task] = {}
        self.chat_lock_by_chat: dict[int, asyncio.Lock] = {}

    def init_config_dir(self):
        """Ensure config directory exists."""
        os.makedirs(CONFIG_DIR, exist_ok=True)

    def install_system_prompts(self):
        """Write CLAUDE.md / AGENTS.md / GEMINI.md into the working directory.

        Each CLI agent reads its own convention file from the cwd to understand
        its role and behaviour.  We ship the canonical prompt as package data
        and materialise it on every ``serve`` so it stays current across
        package upgrades.
        """
        source = importlib.resources.files("operator_agent").joinpath("system_prompt.md")
        content = source.read_text(encoding="utf-8")

        for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
            target = os.path.join(self.working_dir, name)
            if os.path.exists(target):
                continue
            try:
                with open(target, "w") as f:
                    f.write(content)
                log.info("Wrote %s to %s", name, self.working_dir)
            except OSError:
                log.warning("Could not write %s to %s", name, self.working_dir, exc_info=True)

    # --- State persistence ---

    def save_state(self):
        """Atomically persist session state to disk."""
        data = {
            "active_provider_by_chat": {
                str(k): v for k, v in self.active_provider_by_chat.items()
            },
            "active_model_by_chat_provider": {
                f"{chat_id}:{provider}": model
                for (chat_id, provider), model in self.active_model_by_chat_provider.items()
            },
            "session_by_chat_provider": {
                f"{chat_id}:{provider}": sid
                for (chat_id, provider), sid in self.session_by_chat_provider.items()
            },
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
            log.debug(
                "State saved: %d providers, %d sessions",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
            )
        except Exception:
            log.exception("Failed to save state")

    def load_state(self):
        """Load persisted state from disk into memory maps."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            for k, v in data.get("active_provider_by_chat", {}).items():
                self.active_provider_by_chat[int(k)] = v
            for k, v in data.get("active_model_by_chat_provider", {}).items():
                chat_id_str, provider = k.split(":", 1)
                self.active_model_by_chat_provider[(int(chat_id_str), provider)] = v
            for k, v in data.get("session_by_chat_provider", {}).items():
                chat_id_str, provider = k.split(":", 1)
                self.session_by_chat_provider[(int(chat_id_str), provider)] = v
            log.info(
                "Loaded state: %d providers, %d sessions, %d model overrides",
                len(self.active_provider_by_chat),
                len(self.session_by_chat_provider),
                len(self.active_model_by_chat_provider),
            )
        except Exception:
            log.exception("Failed to load state, starting fresh")

    # --- Accessors ---

    def get_active_provider(self, chat_id: int) -> str:
        """Return active provider for chat, defaulting to claude."""
        return self.active_provider_by_chat.get(chat_id, "claude")

    def get_active_model(self, chat_id: int, provider: str) -> str:
        """Return active model for chat+provider, defaulting to first in list."""
        stored = self.active_model_by_chat_provider.get((chat_id, provider))
        if stored and stored in self.models.get(provider, []):
            return stored
        models = self.models.get(provider, [])
        return models[0] if models else "default"

    def get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Get or create a per-chat lock to serialize requests."""
        lock = self.chat_lock_by_chat.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_lock_by_chat[chat_id] = lock
        return lock

    # --- Provider management ---

    def _get_provider_path(self, provider_name: str) -> str:
        providers_cfg = self.config.get("providers", {})
        provider_cfg = providers_cfg.get(provider_name, {})
        return provider_cfg.get("path", provider_name)

    def make_provider(self, provider_name: str) -> BaseProvider:
        """Create a fresh provider instance."""
        path = self._get_provider_path(provider_name)
        return get_provider(provider_name, path)

    # --- Process control ---

    async def stop_chat(self, chat_id: int) -> tuple[bool, str | None]:
        """Stop running process/task for a chat. Returns (had_something, error_msg)."""
        process = self.running_process_by_chat.get(chat_id)
        task = self.running_task_by_chat.get(chat_id)

        if process is None and task is None:
            return False, None

        try:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()

            if task and not task.done():
                task.cancel()

            return True, None
        except Exception as exc:
            return True, str(exc)

    # --- Core streaming ---

    async def run_provider(
        self, provider: BaseProvider, prompt: str, chat_id: int, model: str
    ):
        """Spawn a provider subprocess and yield StreamEvents."""
        session_id = self.session_by_chat_provider.get((chat_id, provider.name))
        cmd = provider.build_command(prompt, model, session_id)

        log.info("[%s] Spawning: %s", provider.name, " ".join(cmd[:6]) + " ...")

        stderr = (
            asyncio.subprocess.STDOUT
            if provider.stderr_to_stdout()
            else asyncio.subprocess.PIPE
        )
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": stderr,
            "cwd": self.working_dir,
        }
        limit = provider.stdout_limit()
        if limit:
            kwargs["limit"] = limit

        process = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        log.info("[%s] Process started, pid=%s", provider.name, process.pid)
        self.running_process_by_chat[chat_id] = process

        event_count = 0
        try:
            assert process.stdout is not None
            async for line in process.stdout:
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue

                raw = provider.parse_line(decoded)
                if raw is None:
                    log.debug("[%s] Skipped line: %s", provider.name, decoded[:200])
                    continue

                event_count += 1
                log.debug(
                    "[%s] Event #%d type=%s",
                    provider.name,
                    event_count,
                    raw.get("type", "?"),
                )

                for stream_event in provider.parse_event(raw):
                    if stream_event.kind == "session" and stream_event.session_id:
                        log.info(
                            "[%s] New session: %s",
                            provider.name,
                            stream_event.session_id,
                        )
                        self.session_by_chat_provider[
                            (chat_id, provider.name)
                        ] = stream_event.session_id
                        self.save_state()
                    yield stream_event

        finally:
            rc = process.returncode
            log.info(
                "[%s] Process pid=%s finished, returncode=%s, events=%d",
                provider.name,
                process.pid,
                rc,
                event_count,
            )
            if rc and rc != 0 and not provider.stderr_to_stdout() and process.stderr:
                stderr_data = await process.stderr.read()
                if stderr_data:
                    stderr_text = stderr_data.decode(errors="replace").strip()[:2000]
                    log.error("[%s] stderr: %s", provider.name, stderr_text)
            current = self.running_process_by_chat.get(chat_id)
            if current is process:
                self.running_process_by_chat.pop(chat_id, None)

    # --- Request handling ---

    async def process_request(
        self,
        provider_name: str,
        prompt: str,
        chat_id: int,
        ctx: TransportContext,
    ):
        """Run a full provider request with status ticker and response delivery."""
        provider = self.make_provider(provider_name)
        model = self.get_active_model(chat_id, provider_name)
        display_name = provider_name.capitalize()

        log.info(
            "[%s] Request from chat_id=%s model=%s: %.80s",
            provider_name,
            chat_id,
            model,
            prompt,
        )

        prefix_base = f"{display_name}/{model}"
        status_msg = await ctx.reply_status(f"[{prefix_base} 0s] Working...")
        state = {"status": "Working...", "elapsed": 0, "provider": prefix_base}
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._run_ticker(ctx, status_msg, state, stop))

        response_parts: list[str] = []
        error_text = ""

        try:
            async for event in self.run_provider(provider, prompt, chat_id, model):
                if event.kind == "status":
                    state["status"] = event.text
                elif event.kind == "response":
                    response_parts.append(event.text)
                elif event.kind == "error":
                    error_text = event.text

            stop.set()
            ticker.cancel()

            await ctx.delete_status(status_msg)

            response_text = provider.format_response(response_parts)
            prefix = f"[{prefix_base} {state['elapsed']}s]"

            if response_text:
                log.info(
                    "[%s] Response (%d chars, %ds)",
                    provider_name,
                    len(response_text),
                    state["elapsed"],
                )
                chunks = split_text(response_text)
                chunks[0] = f"{prefix} {chunks[0]}"
                for chunk in chunks:
                    await ctx.reply(chunk)
            elif error_text:
                log.warning(
                    "[%s] Failed after %ds: %s",
                    provider_name,
                    state["elapsed"],
                    error_text[:200],
                )
                await ctx.reply(f"{prefix} Error: {error_text[:4000]}")
            else:
                log.warning(
                    "[%s] Empty response after %ds", provider_name, state["elapsed"]
                )
                await ctx.reply(f"{prefix} (No response)")

        except asyncio.CancelledError:
            stop.set()
            ticker.cancel()
            with contextlib.suppress(Exception):
                await ctx.edit_status(status_msg, f"[{prefix_base}] Stopped.")
            raise

        except Exception as exc:
            stop.set()
            ticker.cancel()
            log.error(
                "Error processing %s request: %s", provider_name, exc, exc_info=True
            )
            error_msg = str(exc)
            prefix = f"[{prefix_base} {state['elapsed']}s]"
            try:
                await ctx.edit_status(
                    status_msg, f"{prefix} Error: {error_msg[:4000]}"
                )
            except Exception:
                for chunk in split_text(f"{prefix} Error: {error_msg}"):
                    await ctx.reply(chunk)

    async def _run_ticker(
        self,
        ctx: TransportContext,
        status_msg: Any,
        state: dict,
        stop: asyncio.Event,
    ):
        """Background task that updates [Provider Xs] status."""
        while not stop.is_set():
            await asyncio.sleep(1)
            if stop.is_set():
                break
            state["elapsed"] += 1
            text = f"[{state['provider']} {state['elapsed']}s] {state['status']}"
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ctx.edit_status(status_msg, text), timeout=5.0)
