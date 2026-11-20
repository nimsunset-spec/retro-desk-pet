import unittest

from brain.engine import BrainEngine, UNKNOWN_RESPONSE
from core.sensors import SystemMonitor


class ContextualReactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.brain = BrainEngine(seed=1)
        cls.brain._model = None
        cls.brain._vectorizer = None
        cls.brain._training_vectors = None

    def test_fullscreen_known_apps_keep_their_detected_activity(self) -> None:
        self.assertEqual(
            SystemMonitor._activity_for_window("visual studio code - project.py", True),
            "coding",
        )
        self.assertEqual(
            SystemMonitor._activity_for_window("unknown fullscreen application", True),
            "gaming",
        )

    def test_app_mentions_get_matching_activity_reactions_without_ml(self) -> None:
        cases = {
            "I opened Chrome": "browsing",
            "I am listening to Spotify": "music",
            "I opened ChatGPT": "ai_chat",
            "I opened File Explorer": "file_system",
        }
        for text, activity in cases.items():
            with self.subTest(text=text):
                result = self.brain.respond(text)
                self.assertEqual(result["intent"], "activity_comment")
                self.assertIn(
                    result["response"],
                    self.brain.dataset["activity_reactions"][activity]["lines"],
                )

    def test_unrecognized_text_does_not_get_a_random_intent_response(self) -> None:
        result = self.brain.respond("asdf qwerty blorp")
        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["response"], UNKNOWN_RESPONSE)


if __name__ == "__main__":
    unittest.main()
