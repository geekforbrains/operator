from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from operator_ai.config import OPERATOR_DIR
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY
from operator_ai.messages import trim_incomplete_tool_turns

DB_PATH = OPERATOR_DIR / "db" / "operator.db"
logger = logging.getLogger("operator.store")


USERNAME_RE = re.compile(r"^[a-z0-9.\-]{1,64}$")


def _validate_username(username: str) -> None:
    if not USERNAME_RE.match(username):
        msg = (
            f"Invalid username {username!r}: must be 1-64 chars, "
            "lowercase alphanumeric, dots, and hyphens only"
        )
        raise ValueError(msg)


@dataclass
class User:
    username: str
    created_at: float
    identities: list[str]
    roles: list[str]
    timezone: str | None = None


@dataclass
class JobState:
    last_run: float = 0.0
    last_result: str = ""
    last_duration_seconds: float = 0.0
    last_error: str = ""
    run_count: int = 0
    skip_count: int = 0
    gate_count: int = 0
    error_count: int = 0


class Store:
    def __init__(self, path: Path = DB_PATH):
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                message_json TEXT NOT NULL,
                created_at REAL,
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
                last_run REAL NOT NULL DEFAULT 0,
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

        # User / identity / role tables
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                created_at REAL NOT NULL DEFAULT 0,
                timezone TEXT
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

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── Conversations ────────────────────────────────────────────

    def ensure_conversation(self, conversation_id: str) -> None:
        self._conn.execute(
            "INSERT INTO conversations (conversation_id) VALUES (?) "
            "ON CONFLICT(conversation_id) DO NOTHING",
            (conversation_id,),
        )
        self._conn.commit()

    def ensure_system_message(self, conversation_id: str, system_prompt: str) -> None:
        row = self._conn.execute(
            "SELECT id, message_json FROM messages WHERE conversation_id = ? ORDER BY id ASC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO messages (conversation_id, message_json, created_at) VALUES (?, ?, ?)",
                (
                    conversation_id,
                    json.dumps({"role": "system", "content": system_prompt}),
                    time.time(),
                ),
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
            "SELECT id, message_json, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        messages = []
        for row in rows:
            message = json.loads(row["message_json"])
            created_at = row["created_at"]
            if created_at and message.get("role") != "system":
                message[MESSAGE_CREATED_AT_KEY] = float(created_at)
            messages.append(message)
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
        rows: list[tuple[str, str, float | None]] = []
        for message in messages:
            payload = dict(message)
            created_at = payload.pop(MESSAGE_CREATED_AT_KEY, None)
            rows.append(
                (
                    conversation_id,
                    json.dumps(payload),
                    float(created_at) if isinstance(created_at, (int, float)) else time.time(),
                )
            )
        self._conn.executemany(
            "INSERT INTO messages (conversation_id, message_json, created_at) VALUES (?, ?, ?)",
            rows,
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
            "SELECT conversation_id FROM platform_message_index "
            "WHERE transport_name = ? AND platform_message_id = ?",
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

    # ── Users ────────────────────────────────────────────────────

    def add_user(self, username: str) -> None:
        _validate_username(username)
        self._conn.execute(
            "INSERT INTO users (username, created_at) VALUES (?, ?)",
            (username, time.time()),
        )
        self._conn.commit()

    def remove_user(self, username: str) -> bool:
        cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
        self._conn.commit()
        return cur.rowcount > 0

    def get_user(self, username: str) -> User | None:
        row = self._conn.execute(
            "SELECT username, created_at, timezone FROM users WHERE username = ?",
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
            timezone=row["timezone"],
        )

    def list_users(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT username, created_at, timezone FROM users ORDER BY username"
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
                    timezone=row["timezone"],
                )
            )
        return users

    # ── Timezone ──────────────────────────────────────────────────

    def set_user_timezone(self, username: str, timezone: str) -> None:
        """Set a user's timezone. Validates the timezone string."""
        from operator_ai.config import _validate_timezone

        _validate_timezone(timezone)
        self._conn.execute(
            "UPDATE users SET timezone = ? WHERE username = ?",
            (timezone, username),
        )
        self._conn.commit()

    def get_user_timezone(self, username: str) -> str | None:
        row = self._conn.execute(
            "SELECT timezone FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return row["timezone"] if row else None

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


_instance: Store | None = None
_instance_path: Path | None = None


def get_store(path: Path | None = None) -> Store:
    global _instance, _instance_path
    resolved = (path or DB_PATH).expanduser().resolve()
    if _instance is None or _instance_path != resolved:
        if _instance is not None:
            _instance.close()
        _instance = Store(path=resolved)
        _instance_path = resolved
    return _instance


def reset_store() -> None:
    global _instance, _instance_path
    if _instance is not None:
        _instance.close()
        _instance = None
        _instance_path = None
