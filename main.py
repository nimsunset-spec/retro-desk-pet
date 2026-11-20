"""Launch the fully offline retro desktop pet."""

from __future__ import annotations

import itertools
import math
import queue
import random
import sys
import threading
import time
from typing import Any

from PyQt6.QtCore import QObject, QPoint, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication

from brain.engine import BrainEngine
from core.autostart import (
    disable_autostart,
    enable_autostart,
    sync_autostart_path,
)
from core.memory import PetMemory
from core.resources import resource_path
from core.sensors import (
    ActivitySnapshot,
    SystemMonitor,
    SystemNotification,
    Telemetry,
    WindowChangeEvent,
    WindowsNotificationListener,
)
from ui.chat_bubble import ChatBubble
from ui.pet_window import PetWindow


class SensorEventBridge(QObject):
    """Deliver worker-thread sensor events safely to the Qt UI thread."""

    window_changed = pyqtSignal(object)
    telemetry_updated = pyqtSignal(object)
    notification_received = pyqtSignal(object)
    notification_summary = pyqtSignal(int, object)
    status_changed = pyqtSignal(str)


class ReactionWorker:
    """Run dialogue generation and all conversation/history writes off the UI thread."""

    def __init__(
        self,
        brain: BrainEngine,
        memory: PetMemory,
        result_callback: Any,
        error_callback: Any,
        is_activity_current: Any,
    ) -> None:
        self._brain = brain
        self._memory = memory
        self._result_callback = result_callback
        self._error_callback = error_callback
        self._is_activity_current = is_activity_current
        self._last_alert_at: dict[str, float] = {}
        self._tasks: queue.PriorityQueue[
            tuple[int, int, tuple[str, int, Any] | None]
        ] = queue.PriorityQueue()
        self._task_sequence = itertools.count()
        self._thread = threading.Thread(
            target=self._run,
            name="retro-pet-reaction-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, kind: str, token: int, payload: Any = None) -> None:
        priority = 0 if kind in {
            "action",
            "stats",
            "chat_persist",
            "persist_reaction",
        } else 5 if kind == "activity" else 10
        self._tasks.put((priority, next(self._task_sequence), (kind, token, payload)))
        if priority == 0:
            print(
                f"[PET-DEBUG] Queued priority persistence: {kind}; "
                f"backlog={self._tasks.qsize()}.",
                flush=True,
            )

    def _run(self) -> None:
        while True:
            _, _, task = self._tasks.get()
            if task is None:
                return
            kind, token, payload = task
            if kind in {"activity", "ambient", "idle"}:
                print(f"[PET-DEBUG] Worker started background task: {kind}", flush=True)
            try:
                result = self._execute(kind, token, payload)
            except Exception as error:
                self._report_error(
                    f"Background {kind} task failed: {type(error).__name__}: {error}"
                )
                continue
            if (
                kind == "activity"
                and result is not None
                and not self._is_activity_current(token)
            ):
                print("[PET-DEBUG] Skipped activity result superseded in worker.", flush=True)
                continue
            try:
                self._result_callback(token, kind, result)
            except Exception as error:
                self._report_error(
                    f"Delivering {kind} result failed: {type(error).__name__}: {error}"
                )
            if kind == "activity":
                try:
                    self._persist_activity(token, payload, result)
                except Exception as error:
                    self._report_error(
                        f"Persisting activity result failed: "
                        f"{type(error).__name__}: {error}"
                    )

    def _persist_activity(
        self,
        token: int,
        payload: dict[str, Any],
        reaction: dict[str, str] | None,
    ) -> None:
        if not self._is_activity_current(token):
            return
        telemetry = payload["telemetry"]
        if payload["record_switch"]:
            self._memory.record_activity_switch(
                telemetry.activity,
                telemetry.detail,
                telemetry.sampled_at,
            )
        for notification_kind in payload["notifications"]:
            self._memory.record_notification(
                telemetry.app_source or telemetry.activity,
                notification_kind,
            )
        if reaction is not None:
            self._persist_pet_reaction(reaction)

    def _report_error(self, message: str) -> None:
        try:
            self._error_callback(message)
        except Exception as callback_error:
            print(
                f"[PET-DEBUG] Error-signal delivery failed: "
                f"{type(callback_error).__name__}: {callback_error}",
                file=sys.stderr,
                flush=True,
            )

    def _execute(self, kind: str, token: int, payload: Any) -> Any:
        if kind == "activity":
            if not self._is_activity_current(token):
                return None
            return self._brain.context_reaction(payload["telemetry"])

        if kind == "notification":
            event: SystemNotification = payload
            if not self._memory.record_system_notification(
                event.notification_id,
                event.source,
            ):
                return None
            reaction = self._brain.notification_reaction(event.app_name)
            reaction["mood"] = "annoyed"
            self._persist_pet_reaction(reaction)
            return reaction

        if kind == "hardware":
            event, telemetry = payload
            reaction = self._brain.hardware_reaction(event, telemetry)
            if reaction is not None:
                self._memory.record_notification(event, "hardware")
                self._persist_pet_reaction(reaction)
            return reaction

        if kind == "idle":
            self._memory.record_notification("desktop", "idle")
            reaction = self._brain.idle_daydream()
            self._persist_pet_reaction(reaction)
            return reaction

        if kind in {"ambient", "poke"}:
            telemetry = payload
            reaction = (
                self._brain.ambient_reaction(telemetry)
                if kind == "ambient"
                else self._brain.poke_reaction(telemetry, self._memory)
            )
            if reaction is not None:
                reaction["_context_signature"] = repr(telemetry.activity_signature)
                self._persist_pet_reaction(reaction)
            return reaction

        if kind == "notification_count":
            reaction = self._brain.notification_count_reaction(
                int(payload),
                self._memory,
            )
            self._persist_pet_reaction(reaction)
            return reaction

        if kind == "chat":
            text, telemetry = payload
            stats = self._memory.get_stats()
            self._memory.add_chat_log("user", text)
            result = self._brain.respond(
                text,
                telemetry=telemetry,
                pet_stats=stats,
                memory=self._memory,
            )
            result["pet_stats"] = stats
            self._memory.add_chat_log("pet", str(result["response"]))
            self._memory.record_response_phrases(
                str(result.get("opener", "")),
                str(result.get("closer", "")),
            )
            return result

        if kind == "action":
            action, reaction = payload
            stats = self._memory.perform_action(action)
            self._persist_pet_reaction(reaction)
            return {"stats": stats}

        if kind == "stats":
            message = str(payload)
            stats = self._memory.get_stats()
            self._memory.add_chat_log("system", message)
            return {"stats": stats}

        if kind == "persist_reaction":
            self._persist_pet_reaction(payload)
            return None

        if kind == "chat_persist":
            text, response = payload
            self._memory.add_chat_log("user", text)
            self._memory.add_chat_log("pet", str(response["response"]))
            self._memory.record_response_phrases(
                str(response.get("opener", "")),
                str(response.get("closer", "")),
            )
            return None

        if kind == "alerts":
            telemetry = payload
            stats = self._memory.get_stats()
            for alert in self._brain.proactive_alerts(telemetry, stats):
                if time.time() - self._last_alert_at.get(alert, 0.0) < 15 * 60:
                    continue
                self._last_alert_at[alert] = time.time()
                self._memory.add_chat_log("system", alert)
                self._memory.record_notification("pet", "alert")
                return alert
            return None

        if kind == "initial_activity":
            telemetry = payload
            self._memory.record_activity_switch(
                telemetry.activity,
                telemetry.detail,
                telemetry.sampled_at,
            )
            return None

        raise ValueError(f"Unknown background task: {kind}")

    def _persist_pet_reaction(self, reaction: dict[str, str]) -> None:
        self._memory.add_chat_log("pet", reaction["message"])
        self._memory.record_response_phrases(
            reaction.get("opener", ""),
            reaction.get("closer", ""),
        )

    def stop(self) -> None:
        self._tasks.put((100, next(self._task_sequence), None))


class PetApp(QObject):
    """Connect UI, SQLite care state, local brain, and PC telemetry."""

    worker_result = pyqtSignal(int, str, object)
    worker_error = pyqtSignal(str)
    ALERT_COOLDOWN_SECONDS = 15 * 60
    HARDWARE_COOLDOWN_SECONDS = 3 * 60
    ACTIVITY_DEBOUNCE_SECONDS = 1.2
    ACTIVITY_ANTI_SPAM_SECONDS = 3.5
    REACTIVE_ALERT_COOLDOWN_SECONDS = 60.0
    NOTIFICATION_SPEECH_GAP_SECONDS = 12.0
    NOTIFICATION_APP_COOLDOWN_SECONDS = 45.0

    def __init__(self, application: QApplication) -> None:
        super().__init__()
        self.application = application
        self.pet_window = PetWindow("Pixel")
        self.chat_bubble = ChatBubble(self.pet_window)
        self.pet_window.move(40, 100)
        self.pet_window.show()
        self.application.processEvents()

        self.memory = PetMemory()
        self._pet_stats = self.memory.get_stats()
        self.pet_window.set_name(self._pet_stats["name"])
        self.monitor = SystemMonitor(interval_seconds=0.5, window_poll_interval_seconds=0.5)
        self.brain = BrainEngine()
        self.sensor_events = SensorEventBridge()
        self.notification_listener = WindowsNotificationListener(
            self.sensor_events.notification_received.emit,
            self.sensor_events.status_changed.emit,
            self.sensor_events.notification_summary.emit,
        )
        self._last_alert_at: dict[str, float] = {}
        self._pending_activity_reactions: list[tuple[str, dict[str, str]]] = []
        self._activity_debounce_deadline = 0.0
        self._reaction_generation = 0
        self._activity_request_signature: tuple[str, str, str, str] | None = None
        self._pending_activity_signature: tuple[str, str, str, str] | None = None
        self._last_activity_reaction_at = 0.0
        self._last_activity_cooldown_log_at = 0.0
        self._activity_waiting_for_bubble_logged = False
        self._last_reactive_alert_at: dict[str, float] = {}
        self._resource_alert_states = {"high_cpu": False, "high_ram": False}
        self._chat_generation = 0
        self._last_hardware_reaction_at: dict[str, float] = {}
        self._last_notification_reaction_at: dict[str, float] = {}
        self._last_notification_speech_at = 0.0
        self._pending_notifications: list[SystemNotification] = []
        self._activity_sprite_state: str | None = None
        self._current_mood = "bored"
        self._temporary_sprite_state: str | None = None
        self._pending_hardware_events: list[tuple[str, dict[str, Any]]] = []
        self._random = random.Random()

        queued = Qt.ConnectionType.QueuedConnection
        self.sensor_events.window_changed.connect(
            self._handle_window_change,
            queued,
        )
        self.sensor_events.telemetry_updated.connect(
            self._handle_telemetry_update,
            queued,
        )
        self.sensor_events.notification_received.connect(
            self._handle_system_notification,
            queued,
        )
        self.sensor_events.notification_summary.connect(
            self._handle_notification_summary,
            queued,
        )
        self.sensor_events.status_changed.connect(
            self._handle_notification_status,
            queued,
        )
        self.worker_result.connect(self._handle_worker_result, queued)
        self.worker_error.connect(self._handle_worker_error, queued)
        self.pet_window.action_requested.connect(self._perform_action)
        self.pet_window.stats_requested.connect(self._show_stats)
        self.pet_window.autostart_requested.connect(
            self._handle_autostart_toggled
        )
        self.pet_window.clicked.connect(self._handle_poke)
        self.pet_window.double_clicked.connect(self._toggle_chat)
        self.pet_window.position_changed.connect(self._reposition_bubble_if_visible)
        self.chat_bubble.message_submitted.connect(self._handle_chat)

        self.reaction_worker = ReactionWorker(
            self.brain,
            self.memory,
            self.worker_result.emit,
            self.worker_error.emit,
            lambda token: (
                token == self._reaction_generation
                and self._activity_signature(self.monitor.latest_snapshot)
                == self._activity_request_signature
            ),
        )

        self.activity_debounce_timer = QTimer(self.pet_window)
        self.activity_debounce_timer.setSingleShot(True)
        self.activity_debounce_timer.timeout.connect(self._flush_activity_reactions)

        self.monitor.start(
            callback=self.sensor_events.telemetry_updated.emit,
            window_callback=self.sensor_events.window_changed.emit,
        )
        initial_telemetry = self.monitor.get_snapshot()
        self._last_observed_activity = initial_telemetry.activity
        self._last_activity_signature = self._activity_signature(initial_telemetry)
        self.reaction_worker.submit("initial_activity", 0, initial_telemetry)
        self._last_observed_late_night = initial_telemetry.late_night
        self._last_is_charging = initial_telemetry.is_charging
        self._last_unread_count = initial_telemetry.unread_notification_count
        self._resource_alert_states = {
            "high_cpu": initial_telemetry.cpu_percent > 80.0,
            "high_ram": initial_telemetry.ram_percent > 85.0,
        }
        initial_temperature = (
            initial_telemetry.cpu_temperature_c
            if initial_telemetry.cpu_temperature_c is not None
            else initial_telemetry.system_temperature_c
        )
        self._temperature_was_hot = (
            initial_temperature is not None and initial_temperature > 80.0
        )
        now = time.monotonic()
        self._next_idle_daydream_at = now + self._random.uniform(360.0, 480.0)
        self._next_active_commentary_at = now + self._random.uniform(20.0, 30.0)
        self._position_bubble()
        self._sync_pet_state()

        self.state_timer = QTimer(self.pet_window)
        self.state_timer.setInterval(5000)
        self.state_timer.timeout.connect(self._refresh_state)
        self.state_timer.start()

        self.activity_timer = QTimer(self.pet_window)
        self.activity_timer.setInterval(500)
        self.activity_timer.timeout.connect(self._poll_reactive_events)
        self.activity_timer.start()

        self.alert_timer = QTimer(self.pet_window)
        self.alert_timer.setInterval(60_000)
        self.alert_timer.timeout.connect(self._check_alerts)
        self.alert_timer.start()
        if sys.platform == "win32":
            self.notification_listener.start()
        self.application.aboutToQuit.connect(self._stop_sensors)

        if self.brain.backend_error:
            print(f"Brain status: {self.brain.backend_error}", file=sys.stderr)

    def _stop_sensors(self) -> None:
        try:
            self.reaction_worker.stop()
            self.notification_listener.stop()
        finally:
            self.monitor.stop()

    def _position_bubble(self) -> None:
        sprite_position = self.pet_window.mapToGlobal(QPoint(56, 4))
        self.chat_bubble.position_near(sprite_position)

    def _reposition_bubble_if_visible(self) -> None:
        if self.chat_bubble.isVisible():
            self._position_bubble()

    def _current_telemetry(self) -> ActivitySnapshot:
        return self.monitor.get_snapshot()

    def _toggle_chat(self) -> None:
        if self.chat_bubble.isVisible() and self.chat_bubble.is_chat_mode:
            self.chat_bubble.hide_immediately()
            return
        self._position_bubble()
        self.chat_bubble.show_for_chat()

    def _show_reaction(
        self,
        reaction: dict[str, str],
        transient: bool = False,
        immediate: bool = False,
    ) -> None:
        mood = reaction.get("mood", self._current_mood)
        if mood in self.pet_window.MOOD_STATES:
            self._current_mood = mood
            self.pet_window.set_mood(mood)
            self.chat_bubble.set_mood(mood)
        if transient:
            self._set_temporary_sprite(reaction["sprite"])
        else:
            self.pet_window.set_state(reaction["sprite"])
        message = reaction["message"]
        print(
            f"[PET-DEBUG] Displayed: {message[:100]!r} "
            f"(immediate={immediate})",
            flush=True,
        )
        self.chat_bubble.show_message(message, immediate=immediate)
        self._position_bubble()

    def _set_temporary_sprite(self, state: str) -> None:
        self._temporary_sprite_state = state
        self.pet_window.set_state(state)
        QTimer.singleShot(2500, lambda expected=state: self._clear_temporary_sprite(expected))

    def _clear_temporary_sprite(self, expected_state: str) -> None:
        if self._temporary_sprite_state == expected_state:
            self._temporary_sprite_state = None
            self._sync_pet_state()

    def _sync_pet_state(self) -> None:
        stats = self._pet_stats
        self.pet_window.set_name(stats["name"])
        if self._temporary_sprite_state is not None:
            state = self._temporary_sprite_state
        elif self._activity_sprite_state is not None:
            state = self._activity_sprite_state
        elif stats["hunger"] >= 75:
            state = "hungry"
        elif stats["energy"] <= 25:
            state = "sleepy"
        elif self._current_mood in self.pet_window.MOOD_STATES:
            self.pet_window.set_mood(self._current_mood)
            return
        elif stats["happiness"] < 30:
            state = "sassy"
        elif stats["happiness"] >= 70:
            state = "happy"
        else:
            state = "idle"
        self.pet_window.set_state(state)

    @staticmethod
    def _activity_key(telemetry: Telemetry) -> str:
        if telemetry.activity == "coding" and telemetry.late_night:
            return "coding_late_night"
        return telemetry.activity

    def _poll_reactive_events(self) -> None:
        telemetry = self.monitor.get_snapshot()
        if self._activity_signature(telemetry) != self._last_activity_signature:
            self._check_activity(telemetry)
        if self._check_late_night_transition():
            return
        if self._flush_activity_reactions():
            return
        if self._flush_notification_reactions():
            return
        if self._check_hardware_events():
            return
        self._check_idle_daydream(telemetry)

    def _handle_window_change(self, _event: WindowChangeEvent) -> None:
        self._handle_telemetry_update(self.monitor.get_snapshot())

    @pyqtSlot(object)
    def _handle_telemetry_update(self, telemetry: ActivitySnapshot) -> None:
        if self._activity_signature(telemetry) != self._last_activity_signature:
            self._check_activity(telemetry)
        self._check_reactive_alerts(telemetry)

    @pyqtSlot(int, object)
    def _handle_notification_summary(
        self,
        unread_count: int,
        notification_apps: tuple[str, ...],
    ) -> None:
        self.monitor.update_notification_summary(
            unread_count,
            notification_apps,
        )

    def _check_reactive_alerts(self, telemetry: ActivitySnapshot) -> None:
        now = time.monotonic()
        unread = telemetry.unread_notification_count
        if unread > self._last_unread_count:
            if now - self._last_reactive_alert_at.get("notifications", 0.0) >= (
                self.REACTIVE_ALERT_COOLDOWN_SECONDS
            ):
                self._last_reactive_alert_at["notifications"] = now
                self.reaction_worker.submit("notification_count", 0, unread)
        self._last_unread_count = unread

        thresholds = {
            "high_cpu": telemetry.cpu_percent > 80.0,
            "high_ram": telemetry.ram_percent > 85.0,
        }
        for event, is_high in thresholds.items():
            crossed = is_high and not self._resource_alert_states[event]
            self._resource_alert_states[event] = is_high
            if crossed and now - self._last_reactive_alert_at.get(event, 0.0) >= (
                self.REACTIVE_ALERT_COOLDOWN_SECONDS
            ):
                self._last_reactive_alert_at[event] = now
                self.reaction_worker.submit(
                    "hardware",
                    0,
                    (event, telemetry.as_dict()),
                )

    @staticmethod
    def _activity_signature(
        telemetry: ActivitySnapshot,
    ) -> tuple[str, str, str, str]:
        return telemetry.activity_signature

    def _check_activity(self, telemetry: ActivitySnapshot) -> bool:
        activity_changed = telemetry.activity != self._last_observed_activity
        activity_signature = self._activity_signature(telemetry)
        content_changed = activity_signature != self._last_activity_signature
        if not content_changed:
            return False
        print(
            f"[PET-DEBUG] Activity changed: {activity_signature!r}; "
            f"debounce={self.ACTIVITY_DEBOUNCE_SECONDS:.1f}s",
            flush=True,
        )
        entered_late_night_coding = (
            telemetry.activity == "coding"
            and telemetry.late_night
            and not self._last_observed_late_night
        )
        self._last_observed_activity = telemetry.activity
        self._last_activity_signature = activity_signature
        self._last_observed_late_night = telemetry.late_night
        notifications: list[str] = []
        if entered_late_night_coding:
            notifications.append("alert")
        if activity_changed and telemetry.activity != "other":
            notifications.append("activity_change")
        elif telemetry.activity != "other":
            notifications.append("window_change")

        self._activity_sprite_state = None
        self._pending_activity_signature = activity_signature
        self._next_active_commentary_at = time.monotonic() + self._random.uniform(
            20.0,
            30.0,
        )
        self._queue_activity_reaction(
            f"{self._activity_key(telemetry)}:{telemetry.detail[:48]}",
            {},
            record_switch=True,
            notifications=notifications,
        )

        return self._flush_activity_reactions()

    def _check_late_night_transition(self) -> bool:
        telemetry = self.monitor.get_snapshot()
        entered_late_night_coding = (
            telemetry.activity == "coding"
            and telemetry.late_night
            and not self._last_observed_late_night
        )
        left_late_night_coding = (
            telemetry.activity == "coding"
            and not telemetry.late_night
            and self._last_observed_late_night
        )
        self._last_observed_late_night = telemetry.late_night
        if entered_late_night_coding:
            self._queue_activity_reaction(
                "coding_late_night",
                {},
                notifications=["alert"],
            )
        elif left_late_night_coding:
            self._queue_activity_reaction("coding_daytime", {})
        return self._flush_activity_reactions()

    def _handle_system_notification(self, event: SystemNotification) -> None:
        if time.monotonic() - self._last_notification_reaction_at.get(event.source, 0.0) < (
            self.NOTIFICATION_APP_COOLDOWN_SECONDS
        ):
            return
        if any(item.source == event.source for item in self._pending_notifications):
            return
        if len(self._pending_notifications) >= 12:
            self._pending_notifications.pop(0)
        self._pending_notifications.append(event)

    def _flush_notification_reactions(self) -> bool:
        if not self._pending_notifications or self.chat_bubble.isVisible():
            return False
        now = time.monotonic()
        if now - self._last_notification_speech_at < self.NOTIFICATION_SPEECH_GAP_SECONDS:
            return False
        event = self._pending_notifications.pop(0)
        self._last_notification_speech_at = now
        self._last_notification_reaction_at[event.source] = now
        self.reaction_worker.submit("notification", 0, event)
        return True

    def _handle_notification_status(self, message: str) -> None:
        print(f"Notification status: {message}", file=sys.stderr)
        if "enabled" in message.lower():
            return
        if not self.chat_bubble.isVisible():
            self.chat_bubble.show_message(
                "Notification access is off, but I can still react to your app changes."
            )
            self._position_bubble()

    @pyqtSlot(int, str, object)
    def _handle_worker_result(self, token: int, kind: str, result: object) -> None:
        if result is None:
            if kind in {"activity", "ambient", "poke"}:
                print(f"[PET-DEBUG] Skipped {kind} reaction: generator returned no result.")
            return
        if kind == "activity":
            if (
                token != self._reaction_generation
                or self._activity_signature(self.monitor.get_snapshot())
                != self._activity_request_signature
            ):
                print(
                    "[PET-DEBUG] Skipped stale activity reaction "
                    f"(token={token}, current={self._reaction_generation}).",
                    flush=True,
                )
                return
            print("[PET-DEBUG] Activity reaction generated.", flush=True)
            self._activity_sprite_state = result["sprite"]
            self._show_reaction(result)
            return
        if kind in {
            "notification",
            "hardware",
            "idle",
            "ambient",
            "poke",
            "notification_count",
        }:
            print(f"[PET-DEBUG] Generated background reaction: {kind}.", flush=True)
            if kind in {"ambient", "poke"}:
                context_signature = result.pop("_context_signature", "")
                live_signature = repr(
                    self._activity_signature(self.monitor.latest_snapshot)
                )
                if context_signature != live_signature:
                    if kind == "poke":
                        self.reaction_worker.submit(
                            "poke",
                            0,
                            self.monitor.latest_snapshot,
                        )
                    return
            self._show_reaction(result, transient=kind != "activity")
            return
        if kind == "chat":
            if token != self._chat_generation:
                return
            self._pet_stats = result["pet_stats"]
            mood = result.get("mood", "bored")
            if mood in self.pet_window.MOOD_STATES:
                self._current_mood = str(mood)
                self.pet_window.set_mood(str(mood))
                self.chat_bubble.set_mood(str(mood))
            if result.get("sprite") in {"happy", "sleepy", "hungry", "sassy", "idle"}:
                self._set_temporary_sprite(str(result["sprite"]))
            reaction = {
                "message": str(result["response"]),
                "mood": str(mood),
                "sprite": str(result.get("sprite", "idle")),
            }
            self._show_reaction(reaction, immediate=True)
            self._sync_pet_state()
            return
        if kind == "action":
            self._pet_stats = result["stats"]
            self._sync_pet_state()
            return
        if kind == "stats":
            self._pet_stats = result["stats"]
            self._sync_pet_state()
            return
        if kind == "alerts":
            self.chat_bubble.show_message(str(result))
            self._position_bubble()

    @pyqtSlot(str)
    def _handle_worker_error(self, message: str) -> None:
        print(f"[PET-DEBUG] {message}", file=sys.stderr, flush=True)

    def _queue_activity_reaction(
        self,
        key: str,
        reaction: dict[str, str],
        record_switch: bool = False,
        notifications: list[str] | None = None,
    ) -> None:
        self._pending_activity_reactions[:] = [(key, reaction)]
        self._pending_activity_record_switch = record_switch
        self._pending_activity_notifications = list(notifications or ())
        self._reaction_generation += 1
        self._activity_debounce_deadline = time.monotonic() + self.ACTIVITY_DEBOUNCE_SECONDS
        timer = getattr(self, "activity_debounce_timer", None)
        if timer is not None:
            timer.start(round(self.ACTIVITY_DEBOUNCE_SECONDS * 1000))

    def _flush_activity_reactions(self) -> bool:
        if not self._pending_activity_reactions:
            return False
        if self.chat_bubble.isVisible():
            if not self._activity_waiting_for_bubble_logged:
                print(
                    "[PET-DEBUG] Activity reaction pending until the current bubble closes.",
                    flush=True,
                )
                self._activity_waiting_for_bubble_logged = True
            return False
        self._activity_waiting_for_bubble_logged = False
        now = time.monotonic()
        if now < self._activity_debounce_deadline:
            return False
        anti_spam_remaining = self.ACTIVITY_ANTI_SPAM_SECONDS - (
            now - self._last_activity_reaction_at
        )
        if anti_spam_remaining > 0:
            last_log_at = getattr(self, "_last_activity_cooldown_log_at", 0.0)
            if now - last_log_at >= 1.0:
                print(
                    f"[PET-DEBUG] Delaying activity reaction for "
                    f"{anti_spam_remaining:.2f}s anti-spam cooldown.",
                    flush=True,
                )
                self._last_activity_cooldown_log_at = now
            self._activity_debounce_deadline = now + anti_spam_remaining
            timer = getattr(self, "activity_debounce_timer", None)
            if timer is not None:
                timer.start(max(1, math.ceil(anti_spam_remaining * 1000)))
            return False
        telemetry = self.monitor.get_snapshot()
        live_signature = self._activity_signature(telemetry)
        if live_signature != self._pending_activity_signature:
            print(
                "[PET-DEBUG] Skipped activity reaction: live signature changed "
                "before dispatch.",
                flush=True,
            )
            return False

        key, _ = self._pending_activity_reactions[-1]
        self._pending_activity_reactions.clear()
        self._last_observed_activity = telemetry.activity
        self._last_activity_signature = live_signature
        self._last_observed_late_night = telemetry.late_night
        self._activity_request_signature = live_signature
        self._last_activity_reaction_at = now
        print(
            f"[PET-DEBUG] Dispatching activity reaction: {key!r}.",
            flush=True,
        )
        self.reaction_worker.submit(
            "activity",
            self._reaction_generation,
            {
                "key": key,
                "telemetry": telemetry,
                "record_switch": self._pending_activity_record_switch,
                "notifications": tuple(self._pending_activity_notifications),
            },
        )
        self._pending_activity_record_switch = False
        self._pending_activity_notifications = []
        return True

    def _check_hardware_events(self) -> bool:
        telemetry = self.monitor.get_snapshot()
        values = telemetry.as_dict()
        current_temperature = (
            telemetry.cpu_temperature_c
            if telemetry.cpu_temperature_c is not None
            else telemetry.system_temperature_c
        )
        current_temperature_hot = (
            current_temperature is not None and current_temperature > 80.0
        )
        events: list[str] = []
        if current_temperature_hot and not self._temperature_was_hot:
            events.append("hot_temperature")
        self._temperature_was_hot = current_temperature_hot

        if telemetry.is_charging is not None:
            if self._last_is_charging is not None and telemetry.is_charging != self._last_is_charging:
                events.append("charger_plugged" if telemetry.is_charging else "charger_unplugged")
            self._last_is_charging = telemetry.is_charging

        for event in events:
            if not any(
                pending_event == event
                for pending_event, _ in self._pending_hardware_events
            ):
                self._pending_hardware_events.append((event, values))
        if self.chat_bubble.isVisible():
            return False

        now = time.time()
        for index, (event, event_telemetry) in enumerate(self._pending_hardware_events):
            last_seen = self._last_hardware_reaction_at.get(event, 0.0)
            if now - last_seen < self.HARDWARE_COOLDOWN_SECONDS:
                continue
            worker_telemetry: Any = event_telemetry
            self._pending_hardware_events.pop(index)
            self._last_hardware_reaction_at[event] = now
            self.reaction_worker.submit(
                "hardware",
                0,
                (event, worker_telemetry),
            )
            return True
        return False

    def _check_idle_daydream(self, telemetry: ActivitySnapshot) -> None:
        now = time.monotonic()
        if telemetry.activity not in {"other", "idle_desktop"}:
            if now < self._next_active_commentary_at:
                return
            self._next_active_commentary_at = now + self._random.uniform(
                20.0,
                30.0,
            )
            if self.chat_bubble.isVisible() or self._pending_activity_reactions:
                return
            self.reaction_worker.submit("ambient", 0, telemetry)
            return
        if now < self._next_idle_daydream_at:
            return
        self._next_idle_daydream_at = now + self._random.uniform(360.0, 480.0)
        if self._random.random() >= 0.25:
            return
        if self.chat_bubble.isVisible() or self._pending_activity_reactions:
            return
        self.reaction_worker.submit("idle", 0)

    def _refresh_state(self) -> None:
        self._sync_pet_state()
        if self.chat_bubble.isVisible():
            self._position_bubble()

    def _perform_action(self, action: str) -> None:
        self._reaction_generation += 1
        self._pending_activity_reactions.clear()
        self.activity_debounce_timer.stop()
        started = time.perf_counter()
        reaction = self.brain.action_reaction(action)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(
            f"[PET-DEBUG] Triggered: {action}; generated synchronously "
            f"in {elapsed_ms:.2f}ms.",
            flush=True,
        )
        previous_level = int(self._pet_stats["level"])
        self._pet_stats = self.memory.preview_action(self._pet_stats, action)
        if self._pet_stats["level"] > previous_level:
            reaction["message"] += f" Level up! I am now level {self._pet_stats['level']}."
        self._sync_pet_state()
        self._show_reaction(reaction, immediate=True)
        self.reaction_worker.submit(
            "action",
            0,
            (action, reaction),
        )

    def _handle_poke(self) -> None:
        self._reaction_generation += 1
        self._pending_activity_reactions.clear()
        self.activity_debounce_timer.stop()
        snapshot = self.monitor.latest_snapshot
        started = time.perf_counter()
        reaction = self.brain.poke_reaction(snapshot)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(
            f"[PET-DEBUG] Triggered: poke; generated synchronously "
            f"in {elapsed_ms:.2f}ms.",
            flush=True,
        )
        self._show_reaction(reaction, transient=True, immediate=True)
        self.reaction_worker.submit("persist_reaction", 0, reaction)

    def _show_stats(self) -> None:
        self._reaction_generation += 1
        self._pending_activity_reactions.clear()
        self.activity_debounce_timer.stop()
        started = time.perf_counter()
        reaction = self.brain.stats_reaction(dict(self._pet_stats))
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(
            f"[PET-DEBUG] Triggered: stats; generated synchronously "
            f"in {elapsed_ms:.2f}ms.",
            flush=True,
        )
        self._show_reaction(reaction, immediate=True)
        self.reaction_worker.submit("stats", 0, reaction["message"])

    def _handle_autostart_toggled(self, enabled: bool) -> None:
        try:
            changed = enable_autostart() if enabled else disable_autostart()
        except OSError as error:
            print(
                f"[PET-DEBUG] Could not update Windows startup setting: {error}",
                file=sys.stderr,
                flush=True,
            )
            self.pet_window.set_autostart_enabled(not enabled)
            reaction = {
                "sprite": "sassy",
                "mood": "annoyed",
                "message": "I couldn't change the Windows startup setting. Check the registry, okay?",
            }
        else:
            if not changed:
                self.pet_window.set_autostart_enabled(False)
                reaction = {
                    "sprite": "sassy",
                    "mood": "annoyed",
                    "message": "That startup switch only works on Windows. Obviously.",
                }
            elif enabled:
                reaction = {
                    "sprite": "sassy",
                    "mood": "smug",
                    "message": "I'll start with Windows. Not because I missed you; I'm just keeping an eye on you.",
                }
            else:
                reaction = {
                    "sprite": "sassy",
                    "mood": "annoyed",
                    "message": "Fine, I won't start with Windows. I wasn't waiting for you anyway.",
                }

        self._show_reaction(reaction, immediate=True)
        self.reaction_worker.submit("persist_reaction", 0, reaction)

    def _handle_chat(self, text: str) -> None:
        self._reaction_generation += 1
        self._pending_activity_reactions.clear()
        self.activity_debounce_timer.stop()
        self._chat_generation += 1
        telemetry = self.monitor.latest_snapshot
        started = time.perf_counter()
        response = self.brain.respond(
            text,
            telemetry=telemetry.as_dict(),
            pet_stats=dict(self._pet_stats),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        reaction = {
            "message": str(response["response"]),
            "mood": str(response["mood"]),
            "sprite": str(response["sprite"]),
        }
        print(
            f"[PET-DEBUG] Triggered: chat; generated synchronously "
            f"in {elapsed_ms:.2f}ms.",
            flush=True,
        )
        self._show_reaction(reaction, immediate=True)
        self.reaction_worker.submit("chat_persist", 0, (text, response))

    def _check_alerts(self) -> None:
        self.reaction_worker.submit("alerts", 0, self.monitor.get_snapshot().as_dict())


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Retro Desktop Pet")
    application.setQuitOnLastWindowClosed(False)
    try:
        sync_autostart_path()
    except OSError as error:
        print(
            f"[PET-DEBUG] Could not synchronize Windows startup path: {error}",
            file=sys.stderr,
            flush=True,
        )
    assets = resource_path("assets", "sprites")
    if not assets.is_dir():
        raise FileNotFoundError(
            f"Sprites not found at {assets}. Run assets/generate_sprites.py first."
        )
    controller = PetApp(application)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
