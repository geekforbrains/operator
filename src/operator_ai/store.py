from __future__ import annotations

import contextlib
import json
import logging
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

import sqlite_vec

from operator_ai.config import OPERATOR_DIR
from operator_ai.messages import trim_incomplete_tool_turns

DB_PATH = OPERATOR_DIR / "state" / "operator.db"
logger = logging.getLogger("operator.store")


_USERNAME_RE = re.compile(r"^[a-z0-9.\-]{1,64}$")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username):
        msg = (
            f"Invalid username {username!r}: must be 1-64 chars, "
            "lowercase alphanumeric, dots, and hyphens only"
        )
        raise ValueError(msg)


@dataclass
class User:
    username: str
    created_at: str
    identities: list[str]
    roles: list[str]


@dataclass
class JobState:
    last_run: str = ""
    last_result: str = ""
    last_duration_seconds: float = 0
    last_error: str = ""
    run_count: int = 0
    skip_count: int = 0
    gate_count: int = 0
    error_count: int = 0


class Store:
    def __init__(self, path: Path = DB_PATH, embed_dimensions: int = 1536):
        self._path = path
        self._embed_dimensions = embed_dimensions
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                transport_name TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                root_thread_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                message_json TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id, id)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
            ON conversations(updated_at)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_message_index (
                transport_name TEXT NOT NULL,
                platform_message_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                PRIMARY KEY (transport_name, platform_message_id),
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_state (
                job_name TEXT PRIMARY KEY,
                last_run TEXT NOT NULL DEFAULT '',
                last_result TEXT NOT NULL DEFAULT '',
                last_duration_seconds REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                run_count INTEGER NOT NULL DEFAULT 0,
                skip_count INTEGER NOT NULL DEFAULT 0,
                gate_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Migrations — add columns that may not exist in older databases
        for col, typedef in [("error_count", "INTEGER NOT NULL DEFAULT 0")]:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE job_state ADD COLUMN {col} {typedef}")

        # Memory tables
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_scope
            ON memories(scope, scope_id)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_scope_pinned
            ON memories(scope, scope_id, pinned, id)
            """
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._assert_embed_dimensions_compatible()

        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                memory_id INTEGER PRIMARY KEY,
                embedding float[{self._embed_dimensions}],
                scope TEXT partition key,
                scope_id TEXT partition key
            )
            """
        )

        self._conn.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES('embed_dimensions', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(self._embed_dimensions),),
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_kv (
                agent TEXT NOT NULL,
                ns TEXT NOT NULL DEFAULT '',
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                expires_at TEXT,
                PRIMARY KEY (agent, ns, key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_kv_expires
            ON agent_kv(expires_at) WHERE expires_at IS NOT NULL
            """
        )

        # User / identity / role tables
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_identities (
                platform_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                PRIMARY KEY (username, role),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            )
            """
        )

        self._conn.commit()

    def _assert_embed_dimensions_compatible(self) -> None:
        configured = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'embed_dimensions'"
        ).fetchone()
        if configured is not None:
            current = int(configured["value"])
            if current != self._embed_dimensions:
                msg = (
                    "Database embedding dimensions mismatch: "
                    f"database={current}, requested={self._embed_dimensions}. "
                    "Use a database created for this dimension or reset ~/.operator/state/operator.db."
                )
                raise ValueError(msg)
            return

        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_memories'"
        ).fetchone()
        if row is None or not row["sql"]:
            return

        match = re.search(r"embedding\s+float\[(\d+)\]", row["sql"])
        if not match:
            return

        table_dims = int(match.group(1))
        if table_dims != self._embed_dimensions:
            msg = (
                "Existing vec_memories schema dimension mismatch: "
                f"table={table_dims}, requested={self._embed_dimensions}. "
                "Use a matching database or reset ~/.operator/state/operator.db."
            )
            raise ValueError(msg)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── Conversations ────────────────────────────────────────────

    def ensure_conversation(
        self,
        conversation_id: str,
        transport_name: str,
        channel_id: str,
        root_thread_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        meta = json.dumps(metadata if metadata is not None else {})
        if metadata is not None:
            self._conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, transport_name, channel_id, root_thread_id,
                    updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    transport_name=excluded.transport_name,
                    channel_id=excluded.channel_id,
                    root_thread_id=excluded.root_thread_id,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (conversation_id, transport_name, channel_id, root_thread_id, now, meta),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, transport_name, channel_id, root_thread_id,
                    updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    transport_name=excluded.transport_name,
                    channel_id=excluded.channel_id,
                    root_thread_id=excluded.root_thread_id,
                    updated_at=excluded.updated_at
                """,
                (conversation_id, transport_name, channel_id, root_thread_id, now, meta),
            )
        self._conn.commit()

    def ensure_system_message(self, conversation_id: str, system_prompt: str) -> None:
        row = self._conn.execute(
            "SELECT id, message_json FROM messages WHERE conversation_id = ? ORDER BY id ASC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO messages (conversation_id, message_json) VALUES (?, ?)",
                (conversation_id, json.dumps({"role": "system", "content": system_prompt})),
            )
            self._conn.commit()
            return

        first = json.loads(row["message_json"])
        if first.get("role") == "system" and first.get("content") != system_prompt:
            first["content"] = system_prompt
            self._conn.execute(
                "UPDATE messages SET message_json = ? WHERE id = ?",
                (json.dumps(first), row["id"]),
            )
            self._conn.commit()

    # ── Messages ─────────────────────────────────────────────────

    def load_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, message_json FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        messages = [json.loads(row["message_json"]) for row in rows]
        safe_messages = trim_incomplete_tool_turns(messages)

        if len(safe_messages) != len(messages):
            removed = len(messages) - len(safe_messages)
            cutoff_id = rows[len(safe_messages)]["id"]
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND id >= ?",
                (conversation_id, cutoff_id),
            )
            self._conn.commit()
            logger.warning(
                "conversation %s had incomplete tool history; removed %d message(s)",
                conversation_id,
                removed,
            )

        return safe_messages

    def append_messages(self, conversation_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        self._conn.executemany(
            "INSERT INTO messages (conversation_id, message_json) VALUES (?, ?)",
            [(conversation_id, json.dumps(message)) for message in messages],
        )
        self._conn.commit()

    # ── Platform message index ───────────────────────────────────

    def index_platform_message(
        self,
        transport_name: str,
        platform_message_id: str,
        conversation_id: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO platform_message_index (transport_name, platform_message_id, conversation_id)
            VALUES (?, ?, ?)
            ON CONFLICT(transport_name, platform_message_id) DO UPDATE SET
                conversation_id=excluded.conversation_id
            """,
            (transport_name, platform_message_id, conversation_id),
        )
        self._conn.commit()

    def lookup_platform_message(self, transport_name: str, platform_message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT conversation_id FROM platform_message_index WHERE transport_name = ? AND platform_message_id = ?",
            (transport_name, platform_message_id),
        ).fetchone()
        return str(row["conversation_id"]) if row else None

    # ── Job state ────────────────────────────────────────────────

    def load_job_state(self, job_name: str) -> JobState:
        row = self._conn.execute(
            "SELECT * FROM job_state WHERE job_name = ?",
            (job_name,),
        ).fetchone()

        if row is None:
            return JobState()

        return JobState(
            last_run=row["last_run"],
            last_result=row["last_result"],
            last_duration_seconds=row["last_duration_seconds"],
            last_error=row["last_error"],
            run_count=row["run_count"],
            skip_count=row["skip_count"],
            gate_count=row["gate_count"],
            error_count=row["error_count"],
        )

    def save_job_state(self, job_name: str, state: JobState) -> None:
        self._conn.execute(
            """
            INSERT INTO job_state (
                job_name, last_run, last_result, last_duration_seconds,
                last_error, run_count, skip_count, gate_count, error_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                last_run=excluded.last_run,
                last_result=excluded.last_result,
                last_duration_seconds=excluded.last_duration_seconds,
                last_error=excluded.last_error,
                run_count=excluded.run_count,
                skip_count=excluded.skip_count,
                gate_count=excluded.gate_count,
                error_count=excluded.error_count
            """,
            (
                job_name,
                state.last_run,
                state.last_result,
                state.last_duration_seconds,
                state.last_error,
                state.run_count,
                state.skip_count,
                state.gate_count,
                state.error_count,
            ),
        )
        self._conn.commit()

    # ── Memory methods ───────────────────────────────────────────

    def insert_memory(
        self,
        content: str,
        scope: str,
        scope_id: str,
        embedding_bytes: bytes,
        pinned: bool = False,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO memories (content, scope, scope_id, pinned) VALUES (?, ?, ?, ?)",
                (content, scope, scope_id, int(pinned)),
            )
            memory_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO vec_memories (memory_id, embedding, scope, scope_id) VALUES (?, ?, ?, ?)",
                (memory_id, embedding_bytes, scope, scope_id),
            )
        return memory_id  # type: ignore[return-value]

    def update_memory(self, memory_id: int, content: str, embedding_bytes: bytes) -> None:
        with self._conn:
            self._conn.execute("UPDATE memories SET content = ? WHERE id = ?", (content, memory_id))
            self._conn.execute(
                "UPDATE vec_memories SET embedding = ? WHERE memory_id = ?",
                (embedding_bytes, memory_id),
            )

    def delete_memory(self, memory_id: int) -> bool:
        with self._conn:
            self._conn.execute("DELETE FROM vec_memories WHERE memory_id = ?", (memory_id,))
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    def search_memories_vec(
        self,
        embedding_bytes: bytes,
        scope: str,
        scope_id: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT v.memory_id, v.distance, m.content, m.scope, m.scope_id
            FROM vec_memories v
            JOIN memories m ON m.id = v.memory_id
            WHERE v.embedding MATCH ? AND v.scope = ? AND v.scope_id = ? AND k = ?
            ORDER BY v.distance
            """,
            (embedding_bytes, scope, scope_id, top_k),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_memories_multi_scope(
        self,
        embedding_bytes: bytes,
        scopes: list[tuple[str, str]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for scope, scope_id in scopes:
            results.extend(self.search_memories_vec(embedding_bytes, scope, scope_id, top_k))
        results.sort(key=lambda r: r["distance"])
        return results[:top_k]

    def count_memories(self, scope: str, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE scope = ? AND scope_id = ?",
            (scope, scope_id),
        ).fetchone()
        return row["cnt"] if row else 0

    def update_memory_pinned(self, memory_id: int, pinned: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE memories SET pinned = ? WHERE id = ?",
            (int(pinned), memory_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_memories(
        self,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT id, content, scope, scope_id, pinned FROM memories"
        params: list[Any] = []
        conditions: list[str] = []
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if scope_id:
            conditions.append("scope_id = ?")
            params.append(scope_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_pinned_memories(self, scope: str, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, content, scope, scope_id, pinned FROM memories WHERE scope = ? AND scope_id = ? AND pinned = 1 ORDER BY id",
            (scope, scope_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_memories_for_scope(self, scope: str, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, content, scope, scope_id, pinned FROM memories WHERE scope = ? AND scope_id = ? ORDER BY id",
            (scope, scope_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_all_memories_by_scope(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT scope, scope_id, COUNT(*) AS count, SUM(pinned) AS pinned FROM memories GROUP BY scope, scope_id ORDER BY scope, scope_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_distinct_scopes(self) -> list[tuple[str, str]]:
        rows = self._conn.execute("SELECT DISTINCT scope, scope_id FROM memories").fetchall()
        return [(row["scope"], row["scope_id"]) for row in rows]

    def memories_exist_since(self, scope: str, scope_id: str, since_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memories WHERE scope = ? AND scope_id = ? AND id > ? LIMIT 1",
            (scope, scope_id, since_id),
        ).fetchone()
        return row is not None

    def get_max_memory_id(self, scope: str, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(id) AS max_id FROM memories WHERE scope = ? AND scope_id = ?",
            (scope, scope_id),
        ).fetchone()
        return row["max_id"] or 0

    # ── Agent KV store ────────────────────────────────────────────

    _NOT_EXPIRED = "(expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now'))"

    def kv_get(self, agent: str, key: str, ns: str = "") -> str | None:
        row = self._conn.execute(
            f"SELECT value FROM agent_kv WHERE agent = ? AND ns = ? AND key = ? AND {self._NOT_EXPIRED}",
            (agent, ns, key),
        ).fetchone()
        return row["value"] if row else None

    def kv_set(
        self,
        agent: str,
        key: str,
        value: str,
        ns: str = "",
        ttl_hours: int | None = None,
    ) -> None:
        expires_at = None
        if ttl_hours and ttl_hours > 0:
            row = self._conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' hours') AS ea",
                (str(ttl_hours),),
            ).fetchone()
            expires_at = row["ea"]
        self._conn.execute(
            """
            INSERT INTO agent_kv (agent, ns, key, value, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent, ns, key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                expires_at = excluded.expires_at
            """,
            (agent, ns, key, value, expires_at),
        )
        self._conn.commit()

    def kv_delete(self, agent: str, key: str, ns: str = "") -> bool:
        cur = self._conn.execute(
            "DELETE FROM agent_kv WHERE agent = ? AND ns = ? AND key = ?",
            (agent, ns, key),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def kv_list(
        self,
        agent: str,
        ns: str = "",
        prefix: str = "",
    ) -> list[dict[str, Any]]:
        if prefix:
            rows = self._conn.execute(
                f"SELECT key, value, expires_at FROM agent_kv WHERE agent = ? AND ns = ? AND key LIKE ? AND {self._NOT_EXPIRED} ORDER BY key",
                (agent, ns, prefix + "%"),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT key, value, expires_at FROM agent_kv WHERE agent = ? AND ns = ? AND {self._NOT_EXPIRED} ORDER BY key",
                (agent, ns),
            ).fetchall()
        return [dict(row) for row in rows]

    def kv_sweep_expired(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM agent_kv WHERE expires_at IS NOT NULL AND expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        )
        self._conn.commit()
        return cur.rowcount

    # ── Memory state ─────────────────────────────────────────────

    def get_memory_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM memory_state WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row else None

    def set_memory_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def conversations_updated_since(self, since: float) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT conversation_id, transport_name, metadata_json, updated_at FROM conversations WHERE updated_at > ? ORDER BY updated_at ASC",
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Users ────────────────────────────────────────────────────

    def add_user(self, username: str) -> None:
        _validate_username(username)
        self._conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
        self._conn.commit()

    def remove_user(self, username: str) -> bool:
        cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
        self._conn.commit()
        return cur.rowcount > 0

    def get_user(self, username: str) -> User | None:
        row = self._conn.execute(
            "SELECT username, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        identities = [
            r["platform_id"]
            for r in self._conn.execute(
                "SELECT platform_id FROM user_identities WHERE username = ?",
                (username,),
            ).fetchall()
        ]
        roles = [
            r["role"]
            for r in self._conn.execute(
                "SELECT role FROM user_roles WHERE username = ?",
                (username,),
            ).fetchall()
        ]
        return User(
            username=row["username"],
            created_at=row["created_at"],
            identities=identities,
            roles=roles,
        )

    def list_users(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT username, created_at FROM users ORDER BY username"
        ).fetchall()
        users: list[User] = []
        for row in rows:
            uname = row["username"]
            identities = [
                r["platform_id"]
                for r in self._conn.execute(
                    "SELECT platform_id FROM user_identities WHERE username = ?",
                    (uname,),
                ).fetchall()
            ]
            roles = [
                r["role"]
                for r in self._conn.execute(
                    "SELECT role FROM user_roles WHERE username = ?",
                    (uname,),
                ).fetchall()
            ]
            users.append(
                User(
                    username=uname,
                    created_at=row["created_at"],
                    identities=identities,
                    roles=roles,
                )
            )
        return users

    # ── Identities ───────────────────────────────────────────────

    def add_identity(self, username: str, platform_id: str) -> None:
        self._conn.execute(
            "INSERT INTO user_identities (platform_id, username) VALUES (?, ?)",
            (platform_id, username),
        )
        self._conn.commit()

    def remove_identity(self, platform_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM user_identities WHERE platform_id = ?", (platform_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def resolve_username(self, platform_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT username FROM user_identities WHERE platform_id = ?",
            (platform_id,),
        ).fetchone()
        return row["username"] if row else None

    # ── Roles ────────────────────────────────────────────────────

    def add_role(self, username: str, role: str) -> None:
        self._conn.execute(
            "INSERT INTO user_roles (username, role) VALUES (?, ?)",
            (username, role),
        )
        self._conn.commit()

    def remove_role(self, username: str, role: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM user_roles WHERE username = ? AND role = ?",
            (username, role),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_user_roles(self, username: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT role FROM user_roles WHERE username = ? ORDER BY role",
            (username,),
        ).fetchall()
        return [row["role"] for row in rows]


def serialize_float32(vec: list[float]) -> bytes:
    """Pack a list of floats into a little-endian float32 bytes buffer for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


_instance: Store | None = None


def get_store(embed_dimensions: int = 1536) -> Store:
    global _instance
    if _instance is None:
        _instance = Store(embed_dimensions=embed_dimensions)
    elif _instance._embed_dimensions != embed_dimensions:
        msg = (
            "Store already initialized with embed_dimensions="
            f"{_instance._embed_dimensions}, requested={embed_dimensions}"
        )
        raise ValueError(msg)
    return _instance


def reset_store() -> None:
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None
