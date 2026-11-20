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


if __name__ == "__main__":
    unittest.main()
