"""SQLite-backed pet stats, actions, and chat history."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator


class PetMemory:
    """Persist pet state and apply elapsed-time stat decay."""

    _DECAY_PER_HOUR = {
        "hunger": 2.0,
        "energy": -1.5,
        "happiness": -1.0,
    }
    _ACTION_CHANGES = {
        "feed": (-25.0, 2.0, 5.0, 3),
        "pet": (0.0, 0.0, 12.0, 5),
        "play": (5.0, -12.0, 18.0, 8),
    }
    _SCHEMA_VERSION = 4

    def __init__(self, database_path: str | Path | None = None, pet_name: str = "Pixel") -> None:
        migrate_legacy_database = database_path is None
        if database_path is None:
            database_path = self._default_database_path()
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if migrate_legacy_database:
            self._migrate_legacy_database()
        self._initialize(pet_name)

    @staticmethod
    def _default_database_path() -> Path:
        if os.name == "nt":
            data_home = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            root = Path(data_home) if data_home else Path.home() / "AppData" / "Roaming"
            return root / "RetroDeskPet" / "pet.sqlite3"
        data_home = os.environ.get("XDG_DATA_HOME")
        root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        return root / "retro-desk-pet" / "pet.sqlite3"

    def _migrate_legacy_database(self) -> None:
        legacy_path = Path(__file__).resolve().parent.parent / "data" / "pet.sqlite3"
        if self.database_path.exists() or not legacy_path.is_file():
            return
        try:
            with closing(sqlite3.connect(str(legacy_path), timeout=10.0)) as source:
                with closing(sqlite3.connect(str(self.database_path), timeout=10.0)) as target:
                    source.backup(target)
        except sqlite3.DatabaseError as error:
            raise RuntimeError(
                f"Could not migrate existing pet data from {legacy_path} "
                f"to {self.database_path}: {error}"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self, pet_name: str) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pet_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL,
                    hunger REAL NOT NULL DEFAULT 20 CHECK (hunger BETWEEN 0 AND 100),
                    energy REAL NOT NULL DEFAULT 80 CHECK (energy BETWEEN 0 AND 100),
                    happiness REAL NOT NULL DEFAULT 80 CHECK (happiness BETWEEN 0 AND 100),
                    friendship_xp INTEGER NOT NULL DEFAULT 0 CHECK (friendship_xp >= 0),
                    last_updated REAL NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    speaker TEXT NOT NULL,
                    message TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS activity_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('activity_change', 'window_change', 'hardware', 'idle', 'alert')
                    )
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS system_notifications (
                    notification_key TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    source TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS spoken_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    normalized_line TEXT NOT NULL,
                    message TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS response_phrases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    phrase_type TEXT NOT NULL CHECK (phrase_type IN ('opener', 'closer')),
                    normalized_phrase TEXT NOT NULL,
                    phrase TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS activity_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    ended_at REAL
                )"""
            )
            self._ensure_columns(
                connection,
                "pet_stats",
                {
                    "name": "TEXT NOT NULL DEFAULT 'Pixel'",
                    "hunger": "REAL NOT NULL DEFAULT 20",
                    "energy": "REAL NOT NULL DEFAULT 80",
                    "happiness": "REAL NOT NULL DEFAULT 80",
                    "friendship_xp": "INTEGER NOT NULL DEFAULT 0",
                    "last_updated": "REAL NOT NULL DEFAULT 0",
                },
            )
            self._ensure_columns(
                connection,
                "chat_logs",
                {
                    "timestamp": "REAL NOT NULL DEFAULT 0",
                    "speaker": "TEXT NOT NULL DEFAULT 'system'",
                    "message": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                connection,
                "activity_notifications",
                {
                    "timestamp": "REAL NOT NULL DEFAULT 0",
                    "source": "TEXT NOT NULL DEFAULT 'unknown'",
                    "kind": "TEXT NOT NULL DEFAULT 'activity_change'",
                },
            )
            self._ensure_columns(
                connection,
                "system_notifications",
                {
                    "timestamp": "REAL NOT NULL DEFAULT 0",
                    "source": "TEXT NOT NULL DEFAULT 'unknown'",
                },
            )
            stats_columns = self._table_columns(connection, "pet_stats")
            if "id" not in stats_columns:
                raise RuntimeError(
                    "The pet_stats table is missing its required id column; "
                    "the existing database was left unchanged."
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_spoken_lines_timestamp "
                "ON spoken_lines(timestamp DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_response_phrases_type_timestamp "
                "ON response_phrases(phrase_type, timestamp DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_sessions_activity "
                "ON activity_sessions(activity, started_at)"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO pet_stats
                    (id, name, hunger, energy, happiness, friendship_xp, last_updated)
                VALUES (1, ?, 20, 80, 80, 0, ?)
                """,
                (pet_name.strip() or "Pixel", time.time()),
            )
            connection.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        if table not in {
            "pet_stats",
            "chat_logs",
            "activity_notifications",
            "system_notifications",
        }:
            raise ValueError(f"Unsupported migration table: {table}")
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @classmethod
    def _ensure_columns(
        cls,
        connection: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        existing = cls._table_columns(connection, table)
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0.0, min(100.0, value))

    def _apply_decay(self, connection: sqlite3.Connection, now: float) -> None:
        row = connection.execute(
            "SELECT hunger, energy, happiness, last_updated FROM pet_stats WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Pet stats were not initialized.")
        elapsed_hours = max(0.0, now - float(row["last_updated"])) / 3600.0
        values = {
            stat: self._bounded(float(row[stat]) + rate * elapsed_hours)
            for stat, rate in self._DECAY_PER_HOUR.items()
        }
        connection.execute(
            """
            UPDATE pet_stats
            SET hunger = ?, energy = ?, happiness = ?, last_updated = ?
            WHERE id = 1
            """,
            (values["hunger"], values["energy"], values["happiness"], now),
        )

    def get_stats(self) -> dict[str, Any]:
        """Return current stats after applying time-based decay."""
        now = time.time()
        with self._connection() as connection:
            self._apply_decay(connection, now)
            row = connection.execute("SELECT * FROM pet_stats WHERE id = 1").fetchone()
            if row is None:
                raise RuntimeError("Pet stats were not initialized.")
            stats = dict(row)
        stats["level"] = 1 + stats["friendship_xp"] // 100
        return stats

    def set_name(self, name: str) -> None:
        """Set the pet's display name."""
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("Pet name cannot be empty.")
        with self._connection() as connection:
            connection.execute("UPDATE pet_stats SET name = ? WHERE id = 1", (cleaned_name,))

    def perform_action(self, action: str) -> dict[str, Any]:
        """Apply a supported care action and return the updated stats."""
        if action not in self._ACTION_CHANGES:
            raise ValueError(f"Unsupported pet action: {action}")
        hunger, energy, happiness, xp = self._ACTION_CHANGES[action]
        now = time.time()
        with self._connection() as connection:
            self._apply_decay(connection, now)
            connection.execute(
                """
                UPDATE pet_stats
                SET hunger = ?, energy = ?, happiness = ?,
                    friendship_xp = friendship_xp + ?
                WHERE id = 1
                """,
                (
                    self._bounded(self._read_stat(connection, "hunger") + hunger),
                    self._bounded(self._read_stat(connection, "energy") + energy),
                    self._bounded(self._read_stat(connection, "happiness") + happiness),
                    xp,
                ),
            )
        return self.get_stats()

    def preview_action(
        self,
        stats: dict[str, Any],
        action: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Predict an action's visible stat changes without reading or writing SQLite."""
        if action not in self._ACTION_CHANGES:
            raise ValueError(f"Unsupported pet action: {action}")
        timestamp = time.time() if now is None else now
        preview = dict(stats)
        elapsed_hours = max(
            0.0,
            timestamp - float(preview.get("last_updated", timestamp)),
        ) / 3600.0
        for stat, rate in self._DECAY_PER_HOUR.items():
            preview[stat] = self._bounded(
                float(preview[stat]) + rate * elapsed_hours
            )
        hunger, energy, happiness, xp = self._ACTION_CHANGES[action]
        preview["hunger"] = self._bounded(float(preview["hunger"]) + hunger)
        preview["energy"] = self._bounded(float(preview["energy"]) + energy)
        preview["happiness"] = self._bounded(float(preview["happiness"]) + happiness)
        preview["friendship_xp"] = int(preview["friendship_xp"]) + xp
        preview["level"] = 1 + preview["friendship_xp"] // 100
        preview["last_updated"] = timestamp
        return preview

    @staticmethod
    def _read_stat(connection: sqlite3.Connection, stat: str) -> float:
        row = connection.execute(f"SELECT {stat} FROM pet_stats WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("Pet stats were not initialized.")
        return float(row[stat])

    def add_chat_log(self, speaker: str, message: str) -> None:
        """Append one message to the conversation history."""
        if speaker not in {"user", "pet", "system"}:
            raise ValueError("Speaker must be 'user', 'pet', or 'system'.")
        if not message.strip():
            raise ValueError("Chat message cannot be empty.")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO chat_logs (timestamp, speaker, message) VALUES (?, ?, ?)",
                (time.time(), speaker, message.strip()),
            )
            if speaker == "pet":
                cleaned_message = message.strip()
                normalized = " ".join(cleaned_message.casefold().split())
                connection.execute(
                    """
                    INSERT INTO spoken_lines (timestamp, normalized_line, message)
                    VALUES (?, ?, ?)
                    """,
                    (time.time(), normalized, cleaned_message),
                )

    def recent_spoken_lines(self, limit: int = 30) -> list[str]:
        """Return recent pet dialogue for local response anti-repetition checks."""
        if limit < 1:
            raise ValueError("Spoken-line limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT message FROM spoken_lines
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["message"]) for row in rows]

    def recent_response_phrases(
        self,
        phrase_type: str,
        limit: int = 10,
    ) -> list[str]:
        """Return recently spoken opener or closer clauses for anti-repetition."""
        if phrase_type not in {"opener", "closer"}:
            raise ValueError("Phrase type must be 'opener' or 'closer'.")
        if limit < 1:
            raise ValueError("Phrase limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT phrase FROM response_phrases
                WHERE phrase_type = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (phrase_type, limit),
            ).fetchall()
        return [str(row["phrase"]) for row in rows]

    def record_response_phrases(self, opener: str = "", closer: str = "") -> None:
        """Persist the opener and closer from a response that was actually shown."""
        phrases = [
            (phrase_type, " ".join(phrase.split()).strip())
            for phrase_type, phrase in (("opener", opener), ("closer", closer))
            if phrase.strip()
        ]
        if not phrases:
            return
        now = time.time()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO response_phrases
                    (timestamp, phrase_type, normalized_phrase, phrase)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (now, phrase_type, phrase.casefold(), phrase)
                    for phrase_type, phrase in phrases
                ],
            )

    def record_activity_switch(
        self,
        activity: str,
        detail: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Close the current activity session and start a durable new one."""
        normalized_activity = activity.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,48}", normalized_activity):
            raise ValueError("Activity must be a short normalized category.")
        now = time.time() if timestamp is None else float(timestamp)
        cleaned_detail = " ".join(detail.split())[:220]
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE activity_sessions
                SET ended_at = ?
                WHERE ended_at IS NULL
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO activity_sessions (activity, detail, started_at)
                VALUES (?, ?, ?)
                """,
                (normalized_activity, cleaned_detail, now),
            )

    def activity_time_summary(self, limit: int = 10) -> list[dict[str, Any]]:
        """Aggregate recorded duration by activity without exposing window titles."""
        if limit < 1:
            raise ValueError("Activity summary limit must be positive.")
        now = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT activity,
                       SUM(MAX(0, COALESCE(ended_at, ?) - started_at)) AS seconds,
                       COUNT(*) AS sessions
                FROM activity_sessions
                GROUP BY activity
                ORDER BY seconds DESC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_activity_switches(self, window_seconds: float = 7200.0) -> dict[str, int]:
        """Count local activity sessions started within the requested recent window."""
        if window_seconds <= 0:
            raise ValueError("Activity-history window must be positive.")
        cutoff = time.time() - window_seconds
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT activity, COUNT(*) AS sessions
                FROM activity_sessions
                WHERE started_at >= ?
                GROUP BY activity
                """,
                (cutoff,),
            ).fetchall()
        return {str(row["activity"]): int(row["sessions"]) for row in rows}

    def get_chat_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent messages in chronological order."""
        if limit < 1:
            raise ValueError("Chat log limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, speaker, message FROM chat_logs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_notification(self, source: str, kind: str) -> None:
        """Store only a normalized app category and event count, never window text."""
        normalized_source = source.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,40}", normalized_source):
            raise ValueError("Notification source must be a short category name.")
        if kind not in {"activity_change", "window_change", "hardware", "idle", "alert"}:
            raise ValueError(f"Unsupported notification kind: {kind}")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO activity_notifications (timestamp, source, kind)
                VALUES (?, ?, ?)
                """,
                (time.time(), normalized_source, kind),
            )

    def get_notification_summary(self, limit: int = 12) -> list[dict[str, Any]]:
        """Return persisted notification counts grouped by app category and event type."""
        if limit < 1:
            raise ValueError("Notification summary limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT source, kind, COUNT(*) AS count, MAX(timestamp) AS last_seen
                FROM (
                    SELECT source, kind, timestamp FROM activity_notifications
                    UNION ALL
                    SELECT source, 'system_notification' AS kind, timestamp
                    FROM system_notifications
                )
                GROUP BY source, kind
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_system_notification(self, notification_key: str, source: str) -> bool:
        """Persist a notification ID once without storing its title or contents."""
        key = notification_key.strip()
        normalized_source = source.strip().lower()
        if not key or len(key) > 512:
            raise ValueError("Notification key must contain between 1 and 512 characters.")
        if not re.fullmatch(r"[a-z0-9_]{1,40}", normalized_source):
            raise ValueError("Notification source must be a short category name.")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO system_notifications
                    (notification_key, timestamp, source)
                VALUES (?, ?, ?)
                """,
                (key, time.time(), normalized_source),
            )
            return cursor.rowcount == 1

    def get_notification_count(self) -> int:
        """Return the lifetime number of tracked activity and system notifications."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM activity_notifications) +
                    (SELECT COUNT(*) FROM system_notifications) AS count
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("Notification count query returned no result.")
        return int(row["count"])
