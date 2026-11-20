import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from brain.dialogue import (
    ACTIVITIES,
    MOODS,
    MOOD_CLOSERS,
    MOOD_OPENERS,
    compose_candidates,
)
from brain.engine import BrainEngine
from brain.generate_dataset import SCENARIOS, iter_examples, write_dataset
from brain.markov import TrigramScorer, build_ngram_model
from brain.train import train
from core.memory import PetMemory


class TsundereEngineTests(unittest.TestCase):
    def test_each_mood_has_twenty_five_distinct_openers_and_closers(self) -> None:
        for mood in MOODS:
            self.assertGreaterEqual(len(MOOD_OPENERS[mood]), 25)
            self.assertGreaterEqual(len(set(MOOD_OPENERS[mood])), 25)
            self.assertGreaterEqual(len(MOOD_CLOSERS[mood]), 25)
            self.assertGreaterEqual(len(set(MOOD_CLOSERS[mood])), 25)

    def test_composer_provides_at_least_fifty_patterns_per_mood_and_activity(self) -> None:
        for activity in ACTIVITIES:
            for mood in MOODS:
                candidates = compose_candidates(
                    activity,
                    {
                        "title": "a readable title",
                        "artist": "an artist",
                        "app": "the app",
                        "ram_gb": "8.0",
                        "unread_count": 12,
                    },
                    mood,
                )
                self.assertGreaterEqual(len(candidates), 50)
                self.assertEqual(len(set(candidates)), len(candidates))
                for response in candidates:
                    self.assertTrue(response[0].isupper())
                    self.assertIn(response[-1], ".!?")
                    self.assertNotIn("working on working on", response.lower())
                    self.assertNotIn("playing playing", response.lower())

    def test_corpus_generator_covers_all_categories_and_moods(self) -> None:
        examples = list(iter_examples())
        self.assertEqual(len(examples), 20_000)
        self.assertEqual(set(SCENARIOS), set(ACTIVITIES))
        activities = {
            activity
            for example in examples
            for activity in ACTIVITIES
            if f"activity={activity}|" in example["context"]
        }
        moods = {
            mood
            for example in examples
            for mood in MOODS
            if f"mood={mood}|" in example["context"]
        }
        self.assertEqual(activities, set(ACTIVITIES))
        self.assertEqual(moods, set(MOODS))
        self.assertIn("“", examples[0]["response"])

    def test_train_builds_a_small_json_trigram_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "tsundere_corpus.jsonl"
            write_dataset(output, example_count=100)
            model_path = root / "ngram_model.json"
            result_path = train(
                corpus_path=output,
                model_path=model_path,
            )
            model = json.loads(model_path.read_text(encoding="utf-8"))
            model_size = model_path.stat().st_size

        self.assertEqual(result_path, model_path)
        self.assertEqual(model["version"], 1)
        self.assertTrue(model["trigrams"])
        self.assertLess(model_size, 2_000_000)

    def test_context_reactions_are_grounded_grammatical_and_non_repeating(self) -> None:
        brain = BrainEngine(seed=11)
        memory_lines = [
            "Hmph. You are watching “a real video title” on YouTube. Try not to make a habit of it, dummy."
        ]
        brain._scorer = TrigramScorer(
            build_ngram_model(["You are watching a video on YouTube. Hmph."])
        )
        response = brain._select_composed_response(
            "youtube_video",
            {"title": "a real video title", "app": "Google Chrome"},
            "annoyed",
            memory_lines,
        )
        self.assertNotIn(response, memory_lines)
        self.assertIn("a real video title", response)
        self.assertTrue(response.endswith("."))

    def test_context_response_avoids_the_last_ten_openers_and_closers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = PetMemory(Path(temporary_directory) / "pet.sqlite3")
            for index in range(10):
                memory.record_response_phrases(
                    MOOD_OPENERS["smug"][index],
                    MOOD_CLOSERS["smug"][index],
                )
            recent_openers = set(memory.recent_response_phrases("opener", limit=10))
            recent_closers = set(memory.recent_response_phrases("closer", limit=10))

            reaction = BrainEngine(seed=1).context_reaction(
                {"activity": "youtube_video", "detail": "a video", "mood": "judging"},
                memory,
            )

        self.assertNotIn(reaction["opener"], recent_openers)
        self.assertNotIn(reaction["closer"], recent_closers)

    def test_runtime_engine_does_not_import_torch(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from brain.engine import BrainEngine; "
                "BrainEngine(seed=1); print('torch' in sys.modules)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
