import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.memory import PetMemory


class NotificationMemoryTests(unittest.TestCase):
    def test_notification_ids_are_persistently_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = PetMemory(Path(temporary_directory) / "pet.sqlite3")

            self.assertTrue(memory.record_system_notification("app:12:time", "browsing"))
            self.assertFalse(memory.record_system_notification("app:12:time", "browsing"))
            self.assertEqual(memory.get_notification_count(), 1)

    def test_older_partial_schema_is_extended_without_losing_pet_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "pet.sqlite3"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "CREATE TABLE pet_stats (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO pet_stats (id, name) VALUES (1, 'Mochi')"
                )
                connection.commit()
            finally:
                connection.close()

            memory = PetMemory(database_path)
            self.assertEqual(memory.get_stats()["name"], "Mochi")
            self.assertEqual(memory.get_notification_count(), 0)

    def test_pet_lines_and_activity_durations_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = PetMemory(Path(temporary_directory) / "pet.sqlite3")
            memory.add_chat_log("pet", "Hmph, I noticed.")
            memory.record_activity_switch("coding", "main.py", timestamp=100.0)
            memory.record_activity_switch("browsing", "YouTube", timestamp=160.0)

            self.assertEqual(memory.recent_spoken_lines(), ["Hmph, I noticed."])
            durations = {row["activity"]: row["seconds"] for row in memory.activity_time_summary()}
            self.assertAlmostEqual(durations["coding"], 60.0)

    def test_recent_activity_switches_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = PetMemory(Path(temporary_directory) / "pet.sqlite3")
            for index in range(4):
                memory.record_activity_switch(
                    "youtube_video",
                    f"video {index}",
                )
            self.assertEqual(memory.recent_activity_switches()["youtube_video"], 4)

    def test_response_phrase_history_is_persistent_and_separate_by_clause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "pet.sqlite3"
            memory = PetMemory(database_path)
            memory.record_response_phrases("Hmph.", "Try again, dummy.")
            memory.record_response_phrases("Honestly.", "I am not keeping score.")

            reopened = PetMemory(database_path)
            self.assertEqual(
                reopened.recent_response_phrases("opener", limit=1),
                ["Honestly."],
            )
            self.assertEqual(
                reopened.recent_response_phrases("closer", limit=2),
                ["I am not keeping score.", "Try again, dummy."],
            )


if __name__ == "__main__":
    unittest.main()
