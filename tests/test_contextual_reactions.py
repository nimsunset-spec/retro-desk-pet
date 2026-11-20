from dataclasses import replace
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from PyQt6.QtCore import QObject

from brain.engine import BrainEngine, UNKNOWN_RESPONSE
from core.memory import PetMemory
from core.sensors import ActivitySnapshot, SystemMonitor, WindowsNotificationListener
from main import PetApp, ReactionWorker


class ContextualReactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.brain = BrainEngine(seed=1)

    def test_only_known_game_executables_are_classified_as_games(self) -> None:
        self.assertEqual(
            SystemMonitor._activity_for_window("visual studio code - project.py", True),
            "coding",
        )

    def test_snapshot_refresh_requests_never_sample_on_the_calling_thread(self) -> None:
        monitor = SystemMonitor()
        monitor._read_active_window = Mock(
            side_effect=AssertionError("foreground API ran on the caller thread")
        )

        first = monitor.get_snapshot(refresh=True)
        second = monitor.sample_foreground_snapshot()

        self.assertEqual(first, second)
        monitor._read_active_window.assert_not_called()

    def test_window_change_callback_uses_cached_snapshot_only(self) -> None:
        app = PetApp.__new__(PetApp)
        QObject.__init__(app)
        snapshot = Mock()
        app.monitor = Mock()
        app.monitor.get_snapshot.return_value = snapshot
        app._activity_signature = Mock(return_value=("changed",))
        app._last_activity_signature = ()
        app._check_activity = Mock()
        app._check_reactive_alerts = Mock()

        PetApp._handle_window_change(app, Mock())

        app.monitor.get_snapshot.assert_called_once_with()
        app.monitor.sample_foreground_snapshot.assert_not_called()
        app._check_activity.assert_called_once_with(snapshot)

    def test_reaction_generation_and_history_writes_run_on_daemon_worker(self) -> None:
        completed = threading.Event()
        result_threads: list[int] = []
        memory_threads: list[int] = []
        results: list[tuple[int, str, object]] = []
        main_thread = threading.get_ident()

        class FakeMemory:
            def get_stats(self) -> dict[str, int]:
                memory_threads.append(threading.get_ident())
                return {"level": 1}

            def add_chat_log(self, _speaker: str, _message: str) -> None:
                memory_threads.append(threading.get_ident())

            def record_response_phrases(self, _opener: str, _closer: str) -> None:
                memory_threads.append(threading.get_ident())

        class FakeBrain:
            def respond(self, _text: str, **_kwargs: object) -> dict[str, str]:
                result_threads.append(threading.get_ident())
                return {
                    "response": "A complete response.",
                    "mood": "smug",
                    "sprite": "sassy",
                }

        def capture_result(token: int, kind: str, result: object) -> None:
            results.append((token, kind, result))
            completed.set()

        worker = ReactionWorker(
            FakeBrain(),  # type: ignore[arg-type]
            FakeMemory(),  # type: ignore[arg-type]
            capture_result,
            self.fail,
            lambda _token: True,
        )
        try:
            worker.submit("chat", 7, ("hello", {}))
            self.assertTrue(completed.wait(2.0))
        finally:
            worker.stop()

        self.assertEqual(results[0][0:2], (7, "chat"))
        self.assertNotEqual(result_threads[0], main_thread)
        self.assertTrue(memory_threads)
        self.assertTrue(all(thread_id != main_thread for thread_id in memory_threads))
        self.assertTrue(worker._thread.daemon)
        self.assertEqual(
            SystemMonitor._activity_for_window("unknown fullscreen application", True),
            "other",
        )
        self.assertEqual(
            SystemMonitor._activity_for_window(
                "Minecraft",
                True,
                process_name="Minecraft.exe",
            ),
            "gaming",
        )

    def test_window_titles_produce_specific_activity_and_detail(self) -> None:
        music = SystemMonitor._make_window_state(
            "Daft Punk - Instant Crush (Official Video) - YouTube - Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        normal_video = SystemMonitor._make_window_state(
            "How to repair a keyboard - YouTube - Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        pin = SystemMonitor._make_window_state(
            "Pixel art character designs - Pinterest - Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        editor = SystemMonitor._make_window_state(
            "retro-desk-pet - sensors.py - VS Code",
            1,
            1,
            "Code.exe",
            False,
        )

        self.assertEqual(music.activity, "youtube_music")
        self.assertIn("Instant Crush", music.detail)
        self.assertEqual(normal_video.activity, "youtube_video")
        self.assertEqual(pin.activity, "pinterest")
        self.assertIn("Pixel art", pin.detail)
        self.assertEqual(editor.active_filename, "sensors.py")
        self.assertEqual(editor.project_name, "retro-desk-pet")

    def test_foreground_browser_titles_keep_full_entity_and_strip_all_suffixes(self) -> None:
        youtube = SystemMonitor._make_window_state(
            "A very useful keyboard restoration guide - YouTube - Google Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        desktop = SystemMonitor._make_window_state(
            "Program Manager",
            1,
            1,
            "explorer.exe",
            False,
        )
        background_explorer = SystemMonitor._make_window_state(
            "",
            1,
            1,
            "explorer.exe",
            False,
        )

        self.assertEqual(youtube.activity, "youtube_video")
        self.assertEqual(
            youtube.detail,
            "A very useful keyboard restoration guide",
        )
        self.assertEqual(desktop.activity, "idle_desktop")
        self.assertEqual(background_explorer.activity, "idle_desktop")

    def test_music_markers_and_exact_foreground_game_process(self) -> None:
        music = SystemMonitor._make_window_state(
            "An indie track - MV - YouTube - Google Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        game = SystemMonitor._make_window_state(
            "Terraria",
            1,
            1,
            "Terraria.exe",
            False,
        )
        launcher = SystemMonitor._make_window_state(
            "Steam Library",
            1,
            1,
            "steam.exe",
            True,
        )

        self.assertEqual(music.activity, "youtube_music")
        self.assertEqual(game.activity, "gaming")
        self.assertEqual(game.detail, "Terraria")
        self.assertNotEqual(launcher.activity, "gaming")

    def test_zen_browser_titles_are_cleaned_before_site_classification(self) -> None:
        music = SystemMonitor._make_window_state(
            "(3) Artist - Song (MV) - YouTube — Zen Browser",
            1,
            1,
            "zen.exe",
            False,
        )
        pin = SystemMonitor._make_window_state(
            "Pixel art character designs - Pinterest - Zen Browser",
            1,
            1,
            "zen.exe",
            False,
        )
        home = SystemMonitor._make_window_state(
            "Pinterest - Home - Zen Browser",
            1,
            1,
            "zen.exe",
            False,
        )
        ai_chat = SystemMonitor._make_window_state(
            "ChatGPT - Zen Browser",
            1,
            1,
            "zen.exe",
            False,
        )

        self.assertEqual((music.activity, music.detail), ("youtube_music", "Artist - Song (MV)"))
        self.assertEqual((pin.activity, pin.detail), ("pinterest", "Pixel art character designs"))
        self.assertEqual((home.activity, home.detail), ("pinterest", "Pinterest home feed"))
        self.assertEqual((ai_chat.activity, ai_chat.detail), ("ai_chat", "ChatGPT"))

    def test_spotify_window_titles_separate_track_metadata_and_browsing_state(self) -> None:
        track = SystemMonitor._make_window_state(
            "Artist Name - Song Title",
            1,
            1,
            "Spotify.exe",
            False,
        )
        paused = SystemMonitor._make_window_state(
            "Spotify Free",
            1,
            1,
            "Spotify.exe",
            False,
        )
        paused_reaction = self.brain.context_reaction(
            {
                "activity": "spotify",
                "detail": paused.detail,
                "media_state": paused.media_state,
                "media_title": paused.media_title,
            }
        )

        self.assertEqual((track.media_artist, track.media_title), ("Artist Name", "Song Title"))
        self.assertEqual(track.media_state, "playing")
        self.assertEqual((paused.detail, paused.media_title, paused.media_state), ("Spotify", "", "paused"))
        self.assertNotIn("by an artist", paused_reaction["message"].lower())
        self.assertNotIn("spotify free", paused_reaction["message"].lower())

    def test_ai_chat_context_does_not_use_coding_file_templates(self) -> None:
        reaction = self.brain.context_reaction(
            {
                "activity": "ai_chat",
                "app_source": "chatgpt",
                "detail": "Help with Python",
                "process_name": "zen.exe",
                "mood": "judging",
            }
        )

        self.assertIsNotNone(reaction)
        self.assertIn("ChatGPT", reaction["message"])
        self.assertNotIn("file in front", reaction["message"].lower())

    def test_open_coding_process_does_not_reclassify_foreground_browser(self) -> None:
        monitor = SystemMonitor.__new__(SystemMonitor)
        monitor._lock = threading.Lock()
        monitor._media = {"title": "", "artist": "", "state": "unknown", "source": ""}
        monitor._notification_count = 0
        monitor._notification_apps = ()
        monitor._game_started_at = None
        monitor._last_activity = None
        monitor._last_process_audit = time.monotonic()
        monitor._top_processes = ()
        monitor._coding_app_running = True
        monitor._tab_last_active = {}
        monitor._uia_probe = None
        monitor._read_temperatures = lambda: (None, None, None)
        state = SystemMonitor._make_window_state(
            "A keyboard tutorial - YouTube - Chrome",
            1,
            1,
            "chrome.exe",
            False,
        )
        memory = Mock(percent=40, used=4 * 1024**3, total=16 * 1024**3)

        with (
            patch("core.sensors.psutil.cpu_percent", return_value=10.0),
            patch("core.sensors.psutil.virtual_memory", return_value=memory),
            patch("core.sensors.psutil.sensors_battery", return_value=None),
        ):
            snapshot = monitor._sample_telemetry(state, window_changed=False)

        self.assertEqual(snapshot.activity, "youtube_video")

    def test_activity_reaction_is_recomputed_from_live_snapshot_after_debounce(self) -> None:
        telemetry = ActivitySnapshot(
            cpu_percent=10,
            ram_percent=40,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="A fresh video - YouTube - Chrome",
            app_source="chrome",
            window_changed=False,
            activity="youtube_video",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=1.0,
            detail="A fresh video",
        )
        app = PetApp.__new__(PetApp)
        QObject.__init__(app)
        app.chat_bubble = Mock()
        app.chat_bubble.isVisible.return_value = False
        app.monitor = Mock()
        app.monitor.get_snapshot.return_value = telemetry
        app._pending_activity_reactions = [("stale", {"message": "old reaction"})]
        app._activity_debounce_deadline = time.monotonic() + 1.0
        app._pending_activity_record_switch = False
        app._pending_activity_notifications = []
        app._reaction_generation = 1
        app._last_observed_activity = "other"
        app._last_activity_signature = ()
        app._pending_activity_signature = telemetry.activity_signature
        app._last_activity_reaction_at = 0.0
        app._last_observed_late_night = False
        app._activity_sprite_state = None
        app._show_reaction = Mock()
        app._sync_pet_state = Mock()
        app.activity_debounce_timer = Mock()
        app.reaction_worker = Mock()

        self.assertFalse(PetApp._flush_activity_reactions(app))
        app.reaction_worker.submit.assert_not_called()
        self.assertEqual(len(app._pending_activity_reactions), 1)

        app._activity_debounce_deadline = 0.0
        self.assertTrue(PetApp._flush_activity_reactions(app))
        self.assertEqual(app.reaction_worker.submit.call_args.args[0:2], ("activity", 1))
        self.assertEqual(
            app.reaction_worker.submit.call_args.args[2]["telemetry"],
            telemetry,
        )

    def test_activity_reaction_queue_keeps_only_the_latest_window(self) -> None:
        app = PetApp.__new__(PetApp)
        QObject.__init__(app)
        app._pending_activity_reactions = []
        app._reaction_generation = 0
        app._activity_debounce_deadline = 0.0
        app.activity_debounce_timer = Mock()

        PetApp._queue_activity_reaction(app, "old window", {"message": "stale"})
        PetApp._queue_activity_reaction(app, "new window", {"message": "fresh"})

        self.assertEqual(app._pending_activity_reactions, [("new window", {"message": "fresh"})])
        self.assertGreater(app._activity_debounce_deadline, 0)
        self.assertEqual(app.activity_debounce_timer.start.call_args.args[0], 1200)

    def test_activity_reaction_anti_spam_cooldown_is_separate_from_debounce(self) -> None:
        telemetry = ActivitySnapshot(
            cpu_percent=20,
            ram_percent=40,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="Video B - YouTube",
            app_source="chrome",
            window_changed=False,
            activity="youtube_video",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=1.0,
            detail="Video B",
            process_name="chrome.exe",
        )
        app = PetApp.__new__(PetApp)
        QObject.__init__(app)
        app._pending_activity_reactions = [("youtube_video:Video B", {})]
        app._activity_debounce_deadline = 101.0
        app._last_activity_reaction_at = 100.0
        app._pending_activity_signature = telemetry.activity_signature
        app.chat_bubble = Mock()
        app.chat_bubble.isVisible.return_value = False
        app.monitor = Mock()
        app.monitor.get_snapshot.return_value = telemetry
        app.activity_debounce_timer = Mock()
        app.reaction_worker = Mock()

        with patch("main.time.monotonic", return_value=102.0):
            self.assertFalse(PetApp._flush_activity_reactions(app))

        self.assertEqual(app.activity_debounce_timer.start.call_args.args[0], 1500)
        app.reaction_worker.submit.assert_not_called()

    def test_tab_idle_estimates_survive_switching_to_another_app(self) -> None:
        monitor = SystemMonitor.__new__(SystemMonitor)
        monitor._tab_last_active = {}
        monitor._track_tabs(("active tab", "background tab"), "active tab", 10.0)

        unused = monitor._track_tabs((), "", 25.0)

        self.assertEqual(len(unused), 2)
        self.assertEqual(
            {item["title"]: item["idle_seconds"] for item in unused},
            {"active tab": 15.0, "background tab": 15.0},
        )

    def test_context_fallback_mentions_the_current_video_detail(self) -> None:
        telemetry = ActivitySnapshot(
            cpu_percent=10,
            ram_percent=40,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="A keyboard repair guide - YouTube - Chrome",
            app_source="chrome",
            window_changed=True,
            activity="youtube_video",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=1.0,
            detail="A keyboard repair guide",
            mood="judging",
        )
        reaction = self.brain.context_reaction(telemetry)
        self.assertIsNotNone(reaction)
        self.assertIn("keyboard repair guide", reaction["message"].lower())
        self.assertEqual(reaction["mood"], "smug")

    def test_context_reaction_uses_clean_activity_details(self) -> None:
        reaction = self.brain.context_reaction(
            {
                "activity": "youtube_video",
                "detail": "an actual video title",
                "mood": "judging",
            }
        )

        self.assertIn("an actual video title", reaction["message"])
        self.assertNotIn("working on working on", reaction["message"].lower())

    def test_poke_and_notification_count_reactions_are_contextual(self) -> None:
        snapshot = ActivitySnapshot(
            cpu_percent=10,
            ram_percent=40,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="A keyboard repair guide - YouTube",
            app_source="chrome",
            window_changed=False,
            activity="youtube_video",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=1.0,
            detail="A keyboard repair guide",
        )

        poke = self.brain.poke_reaction(snapshot)
        notifications = self.brain.notification_count_reaction(7)

        self.assertIn("keyboard repair guide", poke["message"])
        self.assertIn("7", notifications["message"])
        self.assertEqual(poke["mood"], "annoyed")
        self.assertEqual(notifications["mood"], "annoyed")

    def test_background_music_title_does_not_replace_foreground_activity(self) -> None:
        reaction = self.brain.context_reaction(
            {
                "activity": "coding",
                "detail": "sensors.py in retro-desk-pet",
                "process_name": "Code.exe",
                "media_title": "a song playing in the background",
                "media_artist": "another artist",
                "media_source": "Spotify",
                "mood": "judging",
            }
        )

        self.assertIn("sensors.py in retro-desk-pet", reaction["message"])
        self.assertNotIn("a song playing in the background", reaction["message"])

    def test_context_reaction_calls_out_high_ram_without_torch(self) -> None:
        reaction = self.brain.context_reaction(
            {
                "activity": "youtube_video",
                "detail": "a video",
                "ram_percent": 93,
                "ram_used_gb": 12.4,
                "background_processes": ({"name": "chrome.exe"},),
                "mood": "secretly_caring",
            }
        )

        self.assertIn("12.4 GB", reaction["message"])
        self.assertIn("chrome", reaction["message"].lower())
        self.assertEqual(reaction["mood"], "sleepy")

    def test_activity_signature_detects_same_app_tab_and_media_changes(self) -> None:
        baseline = ActivitySnapshot(
            cpu_percent=20,
            ram_percent=40,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="Video A - YouTube - Zen Browser",
            app_source="zen",
            window_changed=False,
            activity="youtube_video",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=1.0,
            detail="Video A",
            process_name="zen.exe",
        )

        tab_changed = replace(
            baseline,
            active_window_title="Video B - YouTube - Zen Browser",
            detail="Video B",
        )
        song_changed = replace(
            baseline,
            activity="spotify",
            media_title="Song B",
            media_artist="Artist",
            process_name="Spotify.exe",
        )

        self.assertNotEqual(baseline.activity_signature, tab_changed.activity_signature)
        self.assertNotEqual(baseline.activity_signature, song_changed.activity_signature)

    def test_notification_and_resource_alerts_are_thresholded_and_cooldown_gated(self) -> None:
        telemetry = ActivitySnapshot(
            cpu_percent=81,
            ram_percent=86,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=12,
            late_night=False,
            active_window_title="VS Code",
            app_source="code",
            window_changed=False,
            activity="coding",
            last_activity=None,
            fullscreen=False,
            coding_detected=True,
            sampled_at=1.0,
            unread_notification_count=3,
        )
        app = PetApp.__new__(PetApp)
        app._last_unread_count = 2
        app._last_reactive_alert_at = {}
        app._resource_alert_states = {"high_cpu": False, "high_ram": False}
        app.reaction_worker = Mock()

        PetApp._check_reactive_alerts(app, telemetry)
        PetApp._check_reactive_alerts(app, telemetry)

        self.assertEqual(
            [call.args[0:2] for call in app.reaction_worker.submit.call_args_list],
            [
                ("notification_count", 0),
                ("hardware", 0),
                ("hardware", 0),
            ],
        )

    def test_context_fallback_calls_out_repeated_recent_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = PetMemory(Path(temporary_directory) / "pet.sqlite3")
            for index in range(4):
                memory.record_activity_switch("youtube_video", f"video {index}")

            reaction = self.brain.context_reaction(
                {
                    "activity": "youtube_video",
                    "detail": "another video",
                    "mood": "judging",
                },
                memory,
            )

        self.assertIn("detour number 4", reaction["message"])

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
                self.assertTrue(result["response"].endswith("."))
                self.assertIn(result["mood"], {"annoyed", "flustered", "smug", "sleepy", "bored"})

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
