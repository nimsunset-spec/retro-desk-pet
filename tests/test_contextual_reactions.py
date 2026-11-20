import threading
import unittest

from brain.engine import BrainEngine, UNKNOWN_RESPONSE
from core.sensors import SystemMonitor, WindowsNotificationListener


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

    def test_notification_reaction_names_the_app_without_notification_contents(self) -> None:
        reaction = self.brain.notification_reaction("Mail")
        self.assertIn("Mail", reaction["message"])
        self.assertIn(reaction["sprite"], {"idle", "happy", "sleepy", "hungry", "sassy"})

    def test_notification_id_includes_app_and_creation_time(self) -> None:
        class AppInfo:
            app_user_model_id = "example.mail"

        class Notification:
            id = 17
            app_info = AppInfo()
            creation_time = "2025-01-02T03:04:05Z"

        self.assertEqual(
            WindowsNotificationListener._stable_id(Notification()),
            "example.mail:17:2025-01-02T03:04:05Z",
        )

    def test_foreground_state_changes_distinguish_open_focus_and_close(self) -> None:
        monitor = SystemMonitor()
        coding = monitor._make_window_state("VS Code", 10, 100, "Code.exe", False)
        first_event = monitor._record_window_state(coding)
        self.assertIsNotNone(first_event)
        self.assertEqual(first_event.kind, "open")

        another_window = monitor._make_window_state("VS Code", 11, 100, "Code.exe", False)
        focus_event = monitor._record_window_state(another_window)
        self.assertIsNotNone(focus_event)
        self.assertEqual(focus_event.kind, "focus")

        desktop = monitor._make_window_state("", None, None, "", False)
        close_event = monitor._record_window_state(desktop)
        self.assertIsNotNone(close_event)
        self.assertEqual(close_event.kind, "close")

    def test_monitor_emits_foreground_transitions_on_the_fast_poll(self) -> None:
        monitor = SystemMonitor(interval_seconds=30.0, window_poll_interval_seconds=0.01)
        states = [
            monitor._make_window_state("VS Code", 10, 100, "Code.exe", False),
            monitor._make_window_state("Spotify", 20, 200, "Spotify.exe", False),
        ]
        latest_state = states[-1]
        events = []
        transitions_received = threading.Event()

        def read_window():
            nonlocal latest_state
            if states:
                latest_state = states.pop(0)
            return latest_state

        def record_event(event):
            events.append(event)
            if len(events) == 2:
                transitions_received.set()

        monitor._read_active_window = read_window
        monitor._sample_telemetry = lambda state, window_changed: monitor._empty_snapshot()
        monitor.start(window_callback=record_event)
        try:
            self.assertTrue(transitions_received.wait(timeout=1.0))
        finally:
            monitor.stop()

        self.assertEqual([event.kind for event in events], ["open", "switch"])


if __name__ == "__main__":
    unittest.main()
