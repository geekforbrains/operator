from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import string
from abc import ABC, abstractmethod
from datetime import UTC, datetime, tzinfo
from typing import Any

import litellm
from croniter import croniter

from operator_ai.config import CleanerConfig, HarvesterConfig, MemoryConfig, ScheduledTaskConfig
from operator_ai.log_context import set_run_context
from operator_ai.prompts import load_prompt
from operator_ai.store import Store, serialize_float32

logger = logging.getLogger("operator.memory")

# For L2-normalized vectors, cosine similarity 0.9 ≈ L2 distance 0.447
DEDUP_L2_THRESHOLD = math.sqrt(2 * (1 - 0.9))  # ~0.447

# Loaded once from prompts/*.md
_HARVESTER_TEMPLATE = load_prompt("harvester.md")
_CLEANER_TEMPLATE = string.Template(load_prompt("cleaner.md"))
_MEMORY_SCOPE_TAGS = ("[user]", "[agent]", "[global]")
_MAX_HARVEST_CONVERSATION_CHARS = 8000


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _parse_harvested_line(
    raw_line: str,
    user_id: str,
    agent_name: str,
    allow_user_scope: bool,
) -> tuple[str, str, str] | None:
    line = raw_line.strip()
    if line.startswith("-"):
        line = line[1:].strip()
    if not line:
        return None

    for tag in _MEMORY_SCOPE_TAGS:
        if not line.startswith(tag):
            continue
        content = line[len(tag) :].strip()
        if not content:
            return None
        if tag == "[user]":
            if not allow_user_scope or not user_id:
                return None
            return ("user", user_id, content)
        if tag == "[agent]":
            return ("agent", agent_name or "default", content)
        return ("global", "global", content)

    return None


class MemoryStore:
    def __init__(self, store: Store, config: MemoryConfig):
        self._store = store
        self._config = config

    async def embed(self, text: str) -> list[float]:
        resp = await litellm.aembedding(
            model=self._config.embed_model,
            input=[text],
            dimensions=self._config.embed_dimensions,
        )
        vec = resp.data[0]["embedding"]
        return _l2_normalize(vec)

    async def save(
        self,
        content: str,
        scope: str,
        scope_id: str,
        pinned: bool = False,
    ) -> int | None:
        vec = await self.embed(content)
        vec_bytes = serialize_float32(vec)

        # Dedup: search top-1 and update if very similar
        existing = self._store.search_memories_vec(vec_bytes, scope, scope_id, top_k=1)
        if existing and existing[0]["distance"] < DEDUP_L2_THRESHOLD:
            memory_id = existing[0]["memory_id"]
            self._store.update_memory(memory_id, content, vec_bytes)
            logger.debug(
                "Dedup-updated memory %d (distance=%.3f)", memory_id, existing[0]["distance"]
            )
            return memory_id

        # Cap check
        count = self._store.count_memories(scope, scope_id)
        if count >= self._config.max_memories:
            logger.warning(
                "Memory cap reached for %s/%s (%d), skipping",
                scope,
                scope_id,
                count,
            )
            return None

        memory_id = self._store.insert_memory(
            content=content,
            scope=scope,
            scope_id=scope_id,
            embedding_bytes=vec_bytes,
            pinned=pinned,
        )
        logger.debug("Saved memory %d: %s/%s", memory_id, scope, scope_id)
        return memory_id

    async def search(
        self,
        query: str,
        scopes: list[tuple[str, str]],
        top_k: int | None = None,
        min_relevance: float | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or self._config.inject_top_k
        min_relevance = (
            min_relevance if min_relevance is not None else self._config.inject_min_relevance
        )

        vec = await self.embed(query)
        vec_bytes = serialize_float32(vec)

        results = self._store.search_memories_multi_scope(vec_bytes, scopes, top_k)

        # Filter by cosine similarity: for L2-normalized vectors,
        # cosine = 1 - (distance^2 / 2)
        filtered = []
        for r in results:
            cosine = 1 - (r["distance"] ** 2 / 2)
            if cosine >= min_relevance:
                r["relevance"] = round(cosine, 3)
                filtered.append(r)

        return filtered

    def forget(self, memory_id: int) -> bool:
        return self._store.delete_memory(memory_id)

    def list_memories(
        self,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._store.list_memories(scope, scope_id, limit, offset)

    def get_pinned_memories(self, scope: str, scope_id: str) -> list[dict[str, Any]]:
        return self._store.get_pinned_memories(scope, scope_id)


class ScheduledWorker(ABC):
    _label: str = "worker"

    def __init__(
        self,
        memory_store: MemoryStore,
        store: Store,
        config: ScheduledTaskConfig,
        tz: tzinfo = UTC,
    ):
        self._memory_store = memory_store
        self._store = store
        self._config = config
        self._tz = tz
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("%s started (schedule: %s)", type(self).__name__, self._config.schedule)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("%s stopped", type(self).__name__)

    async def _tick_loop(self) -> None:
        set_run_context(agent=self._label)
        try:
            while True:
                await asyncio.sleep(60)
                now = datetime.now(self._tz)
                if not croniter.match(self._config.schedule, now):
                    continue
                try:
                    await self._tick()
                except Exception:
                    logger.exception("tick failed")
        except asyncio.CancelledError:
            return

    @abstractmethod
    async def _tick(self) -> None: ...


class MemoryHarvester(ScheduledWorker):
    _label = "harvester"

    def __init__(
        self,
        memory_store: MemoryStore,
        store: Store,
        config: HarvesterConfig,
        tz: tzinfo = UTC,
    ):
        super().__init__(memory_store, store, config, tz)

    async def _tick(self) -> None:
        watermark_str = self._store.get_memory_state("watermark")
        watermark = float(watermark_str) if watermark_str else 0.0

        conversations = self._store.conversations_updated_since(watermark)
        if not conversations:
            logger.debug("no updated conversations")
            return

        total_extracted = 0
        total_messages = 0
        conversations_reviewed = 0
        new_watermark = watermark
        extraction_failed = False

        for conv in conversations:
            conv_id = conv["conversation_id"]
            metadata = json.loads(conv["metadata_json"]) if conv["metadata_json"] else {}

            user_id = metadata.get("user_id", "")
            is_private = bool(metadata.get("is_private", False))
            agent_name = metadata.get("agent", "")

            messages = self._store.load_messages(conv_id)
            # Filter to user+assistant text only
            text_parts = []
            for msg in messages:
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    text_parts.append(f"{role}: {content}")

            if not text_parts:
                new_watermark = max(new_watermark, conv["updated_at"])
                continue

            conversations_reviewed += 1
            total_messages += len(text_parts)

            conversation_text = "\n".join(text_parts)
            # Truncate to avoid excessive token usage
            if len(conversation_text) > _MAX_HARVEST_CONVERSATION_CHARS:
                conversation_text = conversation_text[-_MAX_HARVEST_CONVERSATION_CHARS:]

            try:
                extracted = await self._extract_memories(
                    conversation_text,
                    user_id,
                    agent_name,
                    allow_user_scope=is_private,
                )
                total_extracted += extracted
            except Exception:
                logger.exception("Failed to extract memories from %s", conv_id)
                extraction_failed = True
                # Stop at first failure so we do not advance the global watermark past
                # a conversation that failed extraction.
                break

            new_watermark = max(new_watermark, conv["updated_at"])

        if extraction_failed:
            logger.warning(
                "extraction failed; watermark advanced to %.6f (skipping failed conversation)",
                new_watermark,
            )

        if new_watermark > watermark:
            self._store.set_memory_state("watermark", str(new_watermark))

        logger.info(
            "reviewed %d conversations, %d messages → %d memories extracted",
            conversations_reviewed,
            total_messages,
            total_extracted,
        )

    async def _extract_memories(
        self,
        conversation_text: str,
        user_id: str,
        agent_name: str,
        *,
        allow_user_scope: bool,
    ) -> int:
        prompt = _HARVESTER_TEMPLATE.replace("{conversation}", conversation_text)

        resp = await litellm.acompletion(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1024,
        )
        output = (resp.choices[0].message.content or "").strip()

        if output == "NONE":
            return 0

        count = 0
        for line in output.splitlines():
            parsed = _parse_harvested_line(line, user_id, agent_name, allow_user_scope)
            if parsed is None:
                continue

            scope, scope_id, content = parsed
            result = await self._memory_store.save(content, scope, scope_id)
            if result is not None:
                count += 1

        return count


class MemoryCleaner(ScheduledWorker):
    _label = "cleaner"

    def __init__(
        self,
        memory_store: MemoryStore,
        store: Store,
        config: CleanerConfig,
        tz: tzinfo = UTC,
    ):
        super().__init__(memory_store, store, config, tz)

    async def _tick(self) -> None:
        scopes = self._store.get_distinct_scopes()
        if not scopes:
            logger.debug("no memory scopes to process")
            return

        cleaned = 0
        for scope, scope_id in scopes:
            state_key = f"cleaner:{scope}:{scope_id}"
            watermark_str = self._store.get_memory_state(state_key)
            watermark = int(watermark_str) if watermark_str else 0

            if not self._store.memories_exist_since(scope, scope_id, watermark):
                continue

            try:
                await self._clean_scope(scope, scope_id)
                new_watermark = self._store.get_max_memory_id(scope, scope_id)
                self._store.set_memory_state(state_key, str(new_watermark))
                cleaned += 1
            except Exception:
                logger.exception("Cleaner failed for %s/%s", scope, scope_id)

        if cleaned:
            logger.info("Cleaner: processed %d scope(s)", cleaned)

    async def _clean_scope(self, scope: str, scope_id: str) -> None:
        memories = self._store.get_all_memories_for_scope(scope, scope_id)
        if len(memories) < 2:
            return

        mem_lines = "\n".join(
            f"[id={m['id']}] {m['content']}" + (" [PINNED]" if m["pinned"] else "")
            for m in memories
        )
        prompt = _CLEANER_TEMPLATE.safe_substitute(memories=mem_lines)

        resp = await litellm.acompletion(
            model=self._config.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2048,
        )
        output = (resp.choices[0].message.content or "").strip()

        # Strip markdown fencing if present
        if output.startswith("```"):
            lines = output.splitlines()
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            output = "\n".join(lines)

        try:
            plan = json.loads(output)
        except json.JSONDecodeError:
            logger.warning("Cleaner: LLM returned invalid JSON for %s/%s", scope, scope_id)
            return

        validated = self._validate_cleaner_plan(plan, memories, scope, scope_id)
        if validated is None:
            return
        kept, added, deleted = validated
        memory_by_id = {m["id"]: m for m in memories}

        # Apply updates to kept memories (content may have changed)
        for item in kept:
            mid = item["id"]
            new_content = item["content"]
            original = memory_by_id[mid]
            if original["content"] != new_content:
                vec = await self._memory_store.embed(new_content)
                vec_bytes = serialize_float32(vec)
                self._store.update_memory(mid, new_content, vec_bytes)

        # Insert new split-out memories
        for item in added:
            await self._memory_store.save(item["content"], scope, scope_id)

        # Delete removed memories
        for mid in deleted:
            self._store.delete_memory(mid)

        logger.info(
            "Cleaner %s/%s: kept=%d, added=%d, deleted=%d",
            scope,
            scope_id,
            len(kept),
            len(added),
            len(deleted),
        )

    def _validate_cleaner_plan(
        self,
        plan: Any,
        memories: list[dict[str, Any]],
        scope: str,
        scope_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]] | None:
        if not isinstance(plan, dict):
            logger.warning("Cleaner: non-object JSON plan for %s/%s", scope, scope_id)
            return None

        raw_keep = plan.get("keep")
        raw_add = plan.get("add")
        raw_delete = plan.get("delete")
        if (
            not isinstance(raw_keep, list)
            or not isinstance(raw_add, list)
            or not isinstance(raw_delete, list)
        ):
            logger.warning("Cleaner: malformed plan keys for %s/%s", scope, scope_id)
            return None

        known_ids = {m["id"] for m in memories}
        pinned_ids = {m["id"] for m in memories if m["pinned"]}
        seen_ids: set[int] = set()

        keep: list[dict[str, Any]] = []
        for item in raw_keep:
            if not isinstance(item, dict):
                logger.warning("Cleaner: invalid keep item for %s/%s", scope, scope_id)
                return None
            mid = item.get("id")
            content = item.get("content")
            if (
                not isinstance(mid, int)
                or mid not in known_ids
                or mid in seen_ids
                or not isinstance(content, str)
                or not content.strip()
            ):
                logger.warning("Cleaner: invalid keep entry for %s/%s", scope, scope_id)
                return None
            keep.append({"id": mid, "content": content.strip()})
            seen_ids.add(mid)

        delete: list[int] = []
        for item in raw_delete:
            if not isinstance(item, int) or item not in known_ids or item in seen_ids:
                logger.warning("Cleaner: invalid delete entry for %s/%s", scope, scope_id)
                return None
            if item in pinned_ids:
                logger.warning(
                    "Cleaner: attempted to delete pinned memory %d in %s/%s",
                    item,
                    scope,
                    scope_id,
                )
                return None
            delete.append(item)
            seen_ids.add(item)

        if seen_ids != known_ids:
            logger.warning(
                "Cleaner: incomplete ID coverage for %s/%s (covered=%d expected=%d)",
                scope,
                scope_id,
                len(seen_ids),
                len(known_ids),
            )
            return None

        add: list[dict[str, Any]] = []
        for item in raw_add:
            if not isinstance(item, dict):
                logger.warning("Cleaner: invalid add item for %s/%s", scope, scope_id)
                return None
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                logger.warning("Cleaner: invalid add content for %s/%s", scope, scope_id)
                return None
            add.append({"content": content.strip()})

        return keep, add, delete
