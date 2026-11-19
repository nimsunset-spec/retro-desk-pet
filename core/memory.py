"""SQLite-backed pet stats, actions, and chat history."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class PetMemory:
    """Persist pet state and apply elapsed-time stat decay."""

    _DECAY_PER_HOUR = {
        "hunger": 2.0,
        "energy": -1.5,
        "happiness": -1.0,
    }

    def __init__(self, database_path: str | Path | None = None, pet_name: str = "Pixel") -> None:
        if database_path is None:
            source_database = Path(__file__).resolve().parent.parent / "data" / "pet.sqlite3"
            if source_database.is_file():
                database_path = source_database
            elif os.name == "nt":
                data_home = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                database_path = (
                    Path(data_home) / "RetroDeskPet" / "pet.sqlite3"
                    if data_home
                    else Path.home() / "AppData" / "Local" / "RetroDeskPet" / "pet.sqlite3"
                )
            else:
                data_home = os.environ.get("XDG_DATA_HOME")
                database_root = (
                    Path(data_home).expanduser()
                    if data_home
                    else Path.home() / ".local" / "share"
                )
                database_path = database_root / "retro-desk-pet" / "pet.sqlite3"
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(pet_name)

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pet_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL,
                    hunger REAL NOT NULL DEFAULT 20 CHECK (hunger BETWEEN 0 AND 100),
                    energy REAL NOT NULL DEFAULT 80 CHECK (energy BETWEEN 0 AND 100),
                    happiness REAL NOT NULL DEFAULT 80 CHECK (happiness BETWEEN 0 AND 100),
                    friendship_xp INTEGER NOT NULL DEFAULT 0 CHECK (friendship_xp >= 0),
                    last_updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    speaker TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('activity_change', 'window_change', 'hardware', 'idle', 'alert')
                    )
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO pet_stats
                    (id, name, hunger, energy, happiness, friendship_xp, last_updated)
                VALUES (1, ?, 20, 80, 80, 0, ?)
                """,
                (pet_name.strip() or "Pixel", time.time()),
            )

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
        changes: dict[str, tuple[float, float, float, int]] = {
            "feed": (-25.0, 2.0, 5.0, 3),
            "pet": (0.0, 0.0, 12.0, 5),
            "play": (5.0, -12.0, 18.0, 8),
        }
        if action not in changes:
            raise ValueError(f"Unsupported pet action: {action}")
        hunger, energy, happiness, xp = changes[action]
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
                FROM activity_notifications
                GROUP BY source, kind
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_notification_count(self) -> int:
        """Return the lifetime number of tracked app/activity notifications."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM activity_notifications"
            ).fetchone()
        if row is None:
            raise RuntimeError("Notification count query returned no result.")
        return int(row["count"])
