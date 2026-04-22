from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MemoryManager:
    def __init__(self, db_path: Path, schema_path: Path) -> None:
        self._db_path = Path(db_path)
        self._schema_path = Path(schema_path)
        self._local = threading.local()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        yield self._local.conn

    def _ensure_schema(self) -> None:
        schema_sql = self._schema_path.read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.executescript(schema_sql)
            connection.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def remember(self, namespace: str, content: str, category: str = "fact") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO facts (timestamp, namespace, category, content)
                VALUES (?, ?, ?, ?)
                """,
                (self._now_iso(), namespace, category, content.strip()),
            )
            connection.commit()

    def recall(self, query: str, namespace: str = "system", limit: int = 5) -> list[dict[str, Any]]:
        wildcard = f"%{query.strip()}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, timestamp, namespace, category, content
                FROM facts
                WHERE namespace = ?
                  AND (? = '' OR content LIKE ? COLLATE NOCASE)
                ORDER BY id DESC
                LIMIT ?
                """,
                (namespace, query.strip(), wildcard, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation (timestamp, user_text, assistant_text)
                VALUES (?, ?, ?)
                """,
                (self._now_iso(), user_text.strip(), assistant_text.strip()),
            )
            connection.commit()

    def recent_conversation(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT turn_id, timestamp, user_text, assistant_text
                FROM conversation
                ORDER BY turn_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # Backwards-compatible helpers for older text-first modules.
    def add_short_term(self, role: str, content: str) -> None:
        if role.lower() == "assistant":
            self.add_turn("", content)
        else:
            self.add_turn(content, "")

    def add_long_term(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        category = str((metadata or {}).get("category", role or "fact"))
        self.remember(namespace="system", content=content, category=category)

    def get_short_term(self) -> str:
        turns = self.recent_conversation(limit=5)
        lines: list[str] = []
        for turn in turns:
            if turn["user_text"]:
                lines.append(f"user: {turn['user_text']}")
            if turn["assistant_text"]:
                lines.append(f"assistant: {turn['assistant_text']}")
        return "\n".join(lines)

    def clear_short_term(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM conversation")
            connection.commit()

    def get_recent_long_term(self, limit: int = 10) -> str:
        rows = self.recall(query="", namespace="system", limit=limit)
        return "\n".join(f"system: {row['content']}" for row in reversed(rows))
