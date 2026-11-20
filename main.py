"""Launch the fully offline retro desktop pet."""

from __future__ import annotations

import random
import sys
import time
from dataclasses import replace
from typing import Any

from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from brain.engine import BrainEngine
from core.memory import PetMemory
from core.resources import resource_path
from core.sensors import (
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
    notification_received = pyqtSignal(object)
    status_changed = pyqtSignal(str)


class PetApp:
    """Connect UI, SQLite care state, local brain, and PC telemetry."""

    ALERT_COOLDOWN_SECONDS = 15 * 60
    HARDWARE_COOLDOWN_SECONDS = 3 * 60
    ACTIVITY_SPEECH_GAP_SECONDS = 8.0
    NOTIFICATION_SPEECH_GAP_SECONDS = 12.0
    NOTIFICATION_APP_COOLDOWN_SECONDS = 45.0

    def __init__(self, application: QApplication) -> None:
        self.application = application
        self.memory = PetMemory()
        self.monitor = SystemMonitor(interval_seconds=5.0, window_poll_interval_seconds=0.5)
        self.brain = BrainEngine()
        self.pet_window = PetWindow(self.memory.get_stats()["name"])
        self.chat_bubble = ChatBubble(self.pet_window)
        self.sensor_events = SensorEventBridge()
        self.notification_listener = WindowsNotificationListener(
            self.sensor_events.notification_received.emit,
            self.sensor_events.status_changed.emit,
        )
        self._last_alert_at: dict[str, float] = {}
        self._last_activity_speech_at = 0.0
        self._pending_activity_reactions: list[tuple[str, dict[str, str]]] = []
        self._last_hardware_reaction_at: dict[str, float] = {}
        self._last_notification_reaction_at: dict[str, float] = {}
        self._last_notification_speech_at = 0.0
        self._pending_notifications: list[SystemNotification] = []
        self._activity_sprite_state: str | None = None
        self._temporary_sprite_state: str | None = None
        self._pending_hardware_events: list[tuple[str, dict[str, Any]]] = []
        self._random = random.Random()

        self.sensor_events.window_changed.connect(self._handle_window_change)
        self.sensor_events.notification_received.connect(self._handle_system_notification)
        self.sensor_events.status_changed.connect(self._handle_notification_status)
        self.pet_window.action_requested.connect(self._perform_action)
        self.pet_window.stats_requested.connect(self._show_stats)
        self.pet_window.clicked.connect(self._toggle_chat)
        self.pet_window.position_changed.connect(self._reposition_bubble_if_visible)
        self.chat_bubble.message_submitted.connect(self._handle_chat)

        self.monitor.start(window_callback=self.sensor_events.window_changed.emit)
        initial_telemetry = self.monitor.get_snapshot()
        self._last_observed_activity = initial_telemetry.activity
        self._last_observed_late_night = initial_telemetry.late_night
        self._last_is_charging = initial_telemetry.is_charging
        self._cpu_was_hot = initial_telemetry.cpu_percent > 80.0
        self._ram_was_high = initial_telemetry.ram_percent > 85.0
        initial_temperature = (
            initial_telemetry.cpu_temperature_c
            if initial_telemetry.cpu_temperature_c is not None
            else initial_telemetry.system_temperature_c
        )
        self._temperature_was_hot = (
            initial_temperature is not None and initial_temperature > 80.0
        )
        self._next_idle_daydream_at = time.monotonic() + self._random.uniform(360.0, 480.0)
        self.pet_window.move(40, 100)
        self.pet_window.show()
        self._position_bubble()
        self._sync_pet_state()

        self.state_timer = QTimer(self.pet_window)
        self.state_timer.setInterval(5000)
        self.state_timer.timeout.connect(self._refresh_state)
        self.state_timer.start()

        self.activity_timer = QTimer(self.pet_window)
        self.activity_timer.setInterval(1000)
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
            self.notification_listener.stop()
        finally:
            self.monitor.stop()

    def _position_bubble(self) -> None:
        sprite_position = self.pet_window.mapToGlobal(QPoint(56, 4))
        self.chat_bubble.position_near(sprite_position)

    def _reposition_bubble_if_visible(self) -> None:
        if self.chat_bubble.isVisible():
            self._position_bubble()

    def _current_telemetry(self) -> Telemetry:
        return self.monitor.get_snapshot()

    def _toggle_chat(self) -> None:
        if self.chat_bubble.isVisible():
            self.chat_bubble.hide_immediately()
            return
        self._position_bubble()
        self.chat_bubble.show_for_chat()

    def _show_reaction(
        self,
        reaction: dict[str, str],
        transient: bool = False,
    ) -> None:
        if transient:
            self._set_temporary_sprite(reaction["sprite"])
        else:
            self.pet_window.set_state(reaction["sprite"])
        message = reaction["message"]
        self.memory.add_chat_log("pet", message)
        self.chat_bubble.show_message(message)
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
        stats = self.memory.get_stats()
        self.pet_window.set_name(stats["name"])
        if self._temporary_sprite_state is not None:
            state = self._temporary_sprite_state
        elif self._activity_sprite_state is not None:
            state = self._activity_sprite_state
        elif stats["hunger"] >= 75:
            state = "hungry"
        elif stats["energy"] <= 25:
            state = "sleepy"
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
        if self._check_late_night_transition():
            return
        if self._flush_activity_reactions():
            return
        if self._flush_notification_reactions():
            return
        if self._check_hardware_events():
            return
        self._check_idle_daydream()

    def _handle_window_change(self, event: WindowChangeEvent) -> None:
        state = event.current
        telemetry = replace(
            self.monitor.get_snapshot(),
            active_window_title=state.title,
            app_source=state.app_source,
            activity=state.activity,
            window_changed=True,
            last_activity=event.previous.activity,
            fullscreen=state.fullscreen,
            coding_detected=state.activity == "coding",
            sampled_at=event.occurred_at,
            window_handle=state.window_handle,
            process_id=state.process_id,
            process_name=state.process_name,
        )
        self._check_activity(telemetry)

    def _check_activity(self, telemetry: Telemetry) -> bool:
        activity_changed = telemetry.activity != self._last_observed_activity
        window_changed = telemetry.window_changed
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
        self._last_observed_activity = telemetry.activity
        self._last_observed_late_night = telemetry.late_night
        notification_source = getattr(telemetry, "app_source", telemetry.activity)

        if activity_changed and telemetry.activity != "other":
            self.memory.record_notification(notification_source, "activity_change")
        elif entered_late_night_coding:
            self.memory.record_notification(notification_source, "alert")

        if activity_changed:
            reaction = self.brain.activity_reaction(
                telemetry.activity,
                late_night=telemetry.late_night,
            )
            self._activity_sprite_state = None if reaction is None else reaction["sprite"]
            if reaction is not None:
                self._queue_activity_reaction(self._activity_key(telemetry), reaction)
            else:
                self._sync_pet_state()
        elif entered_late_night_coding:
            reaction = self.brain.activity_reaction("coding", late_night=True)
            if reaction is not None:
                self._activity_sprite_state = reaction["sprite"]
                self._queue_activity_reaction("coding_late_night", reaction)

        if left_late_night_coding:
            reaction = self.brain.activity_reaction("coding")
            if reaction is not None:
                self._activity_sprite_state = reaction["sprite"]
                self.pet_window.set_state(reaction["sprite"])
        elif not activity_changed and not entered_late_night_coding and window_changed:
            if telemetry.activity != "other":
                self.memory.record_notification(notification_source, "window_change")
                reaction_key = (
                    "browser_tab"
                    if telemetry.activity in {"browsing", "web_search"}
                    else telemetry.activity
                )
                reaction = self.brain.activity_reaction(reaction_key)
                if reaction is not None:
                    self._queue_activity_reaction(reaction_key, reaction)

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
            self.memory.record_notification(telemetry.app_source, "alert")
            reaction = self.brain.activity_reaction("coding", late_night=True)
            if reaction is not None:
                self._activity_sprite_state = reaction["sprite"]
                self._queue_activity_reaction("coding_late_night", reaction)
        elif left_late_night_coding:
            reaction = self.brain.activity_reaction("coding")
            if reaction is not None:
                self._activity_sprite_state = reaction["sprite"]
                self.pet_window.set_state(reaction["sprite"])
        return self._flush_activity_reactions()

    def _handle_system_notification(self, event: SystemNotification) -> None:
        if not self.memory.record_system_notification(event.notification_id, event.source):
            return
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
        self._show_reaction(self.brain.notification_reaction(event.app_name), transient=True)
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

    def _queue_activity_reaction(
        self,
        key: str,
        reaction: dict[str, str],
    ) -> None:
        if self._pending_activity_reactions and self._pending_activity_reactions[-1][0] == key:
            self._pending_activity_reactions[-1] = (key, reaction)
            return
        if len(self._pending_activity_reactions) >= 6:
            self._pending_activity_reactions.pop(0)
        self._pending_activity_reactions.append((key, reaction))

    def _flush_activity_reactions(self) -> bool:
        if not self._pending_activity_reactions or self.chat_bubble.isVisible():
            return False
        now = time.monotonic()
        if now - self._last_activity_speech_at < self.ACTIVITY_SPEECH_GAP_SECONDS:
            return False
        _, reaction = self._pending_activity_reactions.pop(0)
        self._last_activity_speech_at = now
        self._show_reaction(reaction)
        return True

    def _check_hardware_events(self) -> bool:
        telemetry = self.monitor.get_snapshot()
        values = telemetry.as_dict()
        current_cpu_hot = telemetry.cpu_percent > 80.0
        current_ram_high = telemetry.ram_percent > 85.0
        current_temperature = (
            telemetry.cpu_temperature_c
            if telemetry.cpu_temperature_c is not None
            else telemetry.system_temperature_c
        )
        current_temperature_hot = (
            current_temperature is not None and current_temperature > 80.0
        )
        events: list[str] = []
        if current_cpu_hot and not self._cpu_was_hot:
            events.append("high_cpu")
        if current_ram_high and not self._ram_was_high:
            events.append("high_ram")
        if current_temperature_hot and not self._temperature_was_hot:
            events.append("hot_temperature")
        self._cpu_was_hot = current_cpu_hot
        self._ram_was_high = current_ram_high
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
            reaction = self.brain.hardware_reaction(event, event_telemetry)
            self._pending_hardware_events.pop(index)
            if reaction is None:
                continue
            self._last_hardware_reaction_at[event] = now
            self.memory.record_notification(event, "hardware")
            self._show_reaction(reaction, transient=True)
            return True
        return False

    def _check_idle_daydream(self) -> None:
        now = time.monotonic()
        if now < self._next_idle_daydream_at:
            return
        self._next_idle_daydream_at = now + self._random.uniform(360.0, 480.0)
        if self._random.random() >= 0.25:
            return
        if self.chat_bubble.isVisible() or self._pending_activity_reactions:
            return
        self.memory.record_notification("desktop", "idle")
        self._show_reaction(self.brain.idle_daydream(), transient=True)

    def _refresh_state(self) -> None:
        self._sync_pet_state()
        if self.chat_bubble.isVisible():
            self._position_bubble()

    def _perform_action(self, action: str) -> None:
        previous_level = self.memory.get_stats()["level"]
        stats = self.memory.perform_action(action)
        messages = {
            "feed": "Yum! Thanks for the snack.",
            "pet": "That is nice. Friendship XP acquired!",
            "play": "Game on! My energy bar is feeling it.",
        }
        message = messages[action]
        self.memory.add_chat_log("pet", message)
        self.chat_bubble.show_message(message)
        self._sync_pet_state()
        self._position_bubble()
        if stats["level"] > previous_level:
            self.chat_bubble.show_message(f"Level up! I am now level {stats['level']}.")

    def _show_stats(self) -> None:
        stats = self.memory.get_stats()
        message = (
            f"{stats['name']}  LV {stats['level']}\n"
            f"Hunger {stats['hunger']:.0f}%  Energy {stats['energy']:.0f}%\n"
            f"Happiness {stats['happiness']:.0f}%  XP {stats['friendship_xp']}"
        )
        self.memory.add_chat_log("system", message)
        self.chat_bubble.show_message(message)
        self._position_bubble()

    def _handle_chat(self, text: str) -> None:
        telemetry = self._current_telemetry().as_dict()
        stats = self.memory.get_stats()
        self.memory.add_chat_log("user", text)
        result = self.brain.respond(text, telemetry=telemetry, pet_stats=stats)
        self.memory.add_chat_log("pet", result["response"])
        if result.get("sprite") in {"happy", "sleepy", "hungry", "sassy", "idle"}:
            self._set_temporary_sprite(result["sprite"])
        self.chat_bubble.show_message(result["response"])
        self._position_bubble()
        self._sync_pet_state()

    def _check_alerts(self) -> None:
        telemetry = self.monitor.get_snapshot().as_dict()
        stats = self.memory.get_stats()
        alerts = self.brain.proactive_alerts(telemetry, stats)
        now = time.time()
        for alert in alerts:
            last_seen = self._last_alert_at.get(alert, 0.0)
            if now - last_seen >= self.ALERT_COOLDOWN_SECONDS:
                self._last_alert_at[alert] = now
                self.memory.add_chat_log("system", alert)
                self.memory.record_notification("pet", "alert")
                self.chat_bubble.show_message(alert)
                self._position_bubble()
                break


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Retro Desktop Pet")
    application.setQuitOnLastWindowClosed(False)
    assets = resource_path("assets", "sprites")
    if not assets.is_dir():
        raise FileNotFoundError(
            f"Sprites not found at {assets}. Run assets/generate_sprites.py first."
        )
    controller = PetApp(application)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
