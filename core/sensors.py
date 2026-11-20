"""Low-overhead background snapshots of local system telemetry."""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Callable

import psutil

try:
    import pygetwindow
except ImportError as error:
    pygetwindow = None
    _WINDOW_IMPORT_ERROR: str | None = str(error)
else:
    _WINDOW_IMPORT_ERROR = None


@dataclass(frozen=True)
class Telemetry:
    cpu_percent: float
    ram_percent: float
    battery_percent: float | None
    battery_plugged_in: bool | None
    is_charging: bool | None
    cpu_temperature_c: float | None
    system_temperature_c: float | None
    hour: int
    late_night: bool
    active_window_title: str
    app_source: str
    window_changed: bool
    activity: str
    last_activity: str | None
    fullscreen: bool
    coding_detected: bool
    sampled_at: float
    error: str | None = None
    window_handle: int | None = None
    process_id: int | None = None
    process_name: str = ""

    def as_dict(self) -> dict[str, float | int | bool | str | None]:
        """Return a plain dictionary suitable for the brain and UI."""
        return asdict(self)


@dataclass(frozen=True)
class ActiveWindowState:
    """Small foreground-window snapshot used for fast transition detection."""

    window_handle: int | None
    process_id: int | None
    process_name: str
    title: str
    activity: str
    app_source: str
    fullscreen: bool
    error: str | None = None

    @property
    def signature(self) -> tuple[int | None, int | None, str]:
        return (
            self.window_handle,
            self.process_id,
            " ".join(self.title.lower().split()),
        )


@dataclass(frozen=True)
class WindowChangeEvent:
    """A foreground-window transition delivered immediately by the monitor."""

    previous: ActiveWindowState
    current: ActiveWindowState
    kind: str
    occurred_at: float


@dataclass(frozen=True)
class SystemNotification:
    """Notification metadata without notification title or body text."""

    notification_id: str
    app_name: str
    source: str


class SystemMonitor:
    """Sample foreground windows quickly and hardware telemetry less often."""

    _ACTIVITY_KEYWORDS = {
        "gaming": ("steam", "epic games", "valorant", "minecraft", "roblox"),
        "coding": (
            "visual studio code",
            "vs code",
            "vscode",
            "pycharm",
            "cursor",
            "visual studio",
            "sublime",
            "zed editor",
        ),
        "terminal": (
            "powershell",
            "windows terminal",
            "command prompt",
            "cmd",
            "bash",
            "zsh",
            "terminal",
            "konsole",
            "gnome terminal",
            "kitty",
            "alacritty",
            "xterm",
        ),
        "git_github": ("github", "gitlab", "gitkraken", "sourcetree", "git gui"),
        "ai_chat": (
            "chatgpt",
            "claude",
            "gemini",
            "copilot",
            "deepseek",
            "deepseak",
            "groq",
            "perplexity",
        ),
        "youtube_video": ("youtube", "netflix", "vlc", "prime video", "primevideo", "twitch"),
        "music": ("spotify", "apple music", "soundcloud", "tidal", "deezer"),
        "social_chat": ("discord", "whatsapp", "telegram", "reddit", "twitter", " x "),
        "docs_study": (
            "notion",
            "obsidian",
            "pdf",
            "word",
            "google docs",
            "stackoverflow",
            "stack overflow",
            "libreoffice writer",
            "okular",
            "evince",
        ),
        "design_art": ("figma", "photoshop", "blender", "aseprite", "canva", "krita", "gimp"),
        "file_system": (
            "file explorer",
            "recycle bin",
            "recyclebin",
            "trash",
            "file manager",
            "nautilus",
            "dolphin",
            "thunar",
            "nemo",
            "pcmanfm",
            "caja",
            "task manager",
            "system monitor",
            " files ",
        ),
        "text_editor": ("notepad", "notepad++", "gedit", "kate", "text editor", "mousepad"),
        "browsing": ("chrome", "chromium", "edge", "msedge", "firefox", "brave", "opera", "vivaldi"),
    }
    _BROWSER_KEYWORDS = _ACTIVITY_KEYWORDS["browsing"]
    _SEARCH_KEYWORDS = ("google search", "bing", "duckduckgo", "search results", "startpage")

    def __init__(
        self,
        interval_seconds: float = 5.0,
        window_poll_interval_seconds: float = 0.5,
    ) -> None:
        if interval_seconds <= 0 or window_poll_interval_seconds <= 0:
            raise ValueError("Monitor intervals must be greater than zero.")
        self.interval_seconds = interval_seconds
        self.window_poll_interval_seconds = min(window_poll_interval_seconds, 1.0)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: Telemetry | None = None
        self._callback: Callable[[Telemetry], None] | None = None
        self._window_callback: Callable[[WindowChangeEvent], None] | None = None
        self._last_activity: str | None = None
        self._current_activity: str | None = None
        self._active_window_state: ActiveWindowState | None = None
        self._screen_size: tuple[int, int] | None = None

    def sample(self) -> Telemetry:
        """Synchronously read hardware and foreground state for explicit refreshes."""
        state = self._read_active_window()
        if state.error:
            with self._lock:
                previous = self._active_window_state
            if previous is not None:
                state = replace(previous, error=state.error)
        event = self._record_window_state(state)
        snapshot = self._sample_telemetry(state, window_changed=event is not None)
        with self._lock:
            self._snapshot = snapshot
            callback = self._window_callback
        if event is not None and callback is not None:
            callback(event)
        return snapshot

    def _sample_telemetry(
        self,
        state: ActiveWindowState,
        window_changed: bool,
    ) -> Telemetry:
        errors: list[str] = []
        cpu_percent = psutil.cpu_percent(interval=None)
        ram_percent = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        battery_percent = None if battery is None else float(battery.percent)
        charging_state = None if battery is None else battery.power_plugged
        is_charging = None if charging_state is None else bool(charging_state)
        battery_plugged_in = is_charging
        cpu_temperature_c, system_temperature_c, temperature_error = self._read_temperatures()
        if temperature_error:
            errors.append(temperature_error)
        if state.error:
            errors.append(state.error)
        hour = datetime.now().hour
        late_night = hour >= 23 or hour < 4
        return Telemetry(
            cpu_percent=float(cpu_percent),
            ram_percent=float(ram_percent),
            battery_percent=battery_percent,
            battery_plugged_in=battery_plugged_in,
            is_charging=is_charging,
            cpu_temperature_c=cpu_temperature_c,
            system_temperature_c=system_temperature_c,
            hour=hour,
            late_night=late_night,
            active_window_title=state.title,
            app_source=state.app_source,
            window_changed=window_changed,
            activity=state.activity,
            last_activity=self._last_activity,
            fullscreen=state.fullscreen,
            coding_detected=state.activity == "coding",
            sampled_at=time.time(),
            error="; ".join(errors) if errors else None,
            window_handle=state.window_handle,
            process_id=state.process_id,
            process_name=state.process_name,
        )

    def _read_active_window(self) -> ActiveWindowState:
        try:
            if sys.platform == "win32":
                return self._read_windows_active_window()
            return self._read_desktop_active_window()
        except (OSError, RuntimeError, subprocess.SubprocessError, psutil.Error) as error:
            return self._make_window_state(
                title="",
                window_handle=None,
                process_id=None,
                process_name="",
                fullscreen=False,
                error=f"Window telemetry unavailable: {type(error).__name__}: {error}",
            )

    def _read_windows_active_window(self) -> ActiveWindowState:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        window_handle = user32.GetForegroundWindow()
        if not window_handle:
            return self._make_window_state("", None, None, "", False)
        title_length = user32.GetWindowTextLengthW(window_handle)
        title_buffer = ctypes.create_unicode_buffer(max(title_length + 1, 2))
        user32.GetWindowTextW(window_handle, title_buffer, len(title_buffer))
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
        process_id_value = int(process_id.value) or None
        process_name = ""
        if process_id_value is not None:
            try:
                process_name = psutil.Process(process_id_value).name()
            except psutil.Error:
                process_name = ""

        rectangle = wintypes.RECT()
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        fullscreen = False
        if user32.GetWindowRect(window_handle, ctypes.byref(rectangle)):
            screen_width = int(user32.GetSystemMetrics(0))
            screen_height = int(user32.GetSystemMetrics(1))
            fullscreen = (
                rectangle.left <= 2
                and rectangle.top <= 2
                and rectangle.right - rectangle.left >= screen_width - 2
                and rectangle.bottom - rectangle.top >= screen_height - 2
            )
        return self._make_window_state(
            title=title_buffer.value,
            window_handle=int(window_handle),
            process_id=process_id_value,
            process_name=process_name,
            fullscreen=fullscreen,
        )

    def _read_desktop_active_window(self) -> ActiveWindowState:
        active_window = None
        title = ""
        if pygetwindow is not None:
            try:
                title = pygetwindow.getActiveWindowTitle() or ""
                get_active_window = getattr(pygetwindow, "getActiveWindow", None)
                if get_active_window is not None:
                    active_window = get_active_window()
            except (OSError, RuntimeError) as error:
                if not sys.platform.startswith("linux"):
                    raise
                title_error = str(error)
            else:
                title_error = None
        else:
            title_error = _WINDOW_IMPORT_ERROR

        if sys.platform.startswith("linux") and not title:
            try:
                title = self._read_linux_active_title()
                title_error = None
            except (OSError, subprocess.SubprocessError) as error:
                title_error = str(error)

        window_handle = getattr(active_window, "_hWnd", None)
        fullscreen = self._is_fullscreen(active_window)
        return self._make_window_state(
            title=title,
            window_handle=int(window_handle) if window_handle is not None else None,
            process_id=None,
            process_name="",
            fullscreen=fullscreen,
            error=(
                f"Window telemetry unavailable: {title_error}"
                if title_error and not sys.platform.startswith("linux")
                else None
            ),
        )

    @classmethod
    def _make_window_state(
        cls,
        title: str,
        window_handle: int | None,
        process_id: int | None,
        process_name: str,
        fullscreen: bool,
        error: str | None = None,
    ) -> ActiveWindowState:
        combined_title = f"{process_name} {title}".strip()
        activity = cls._activity_for_window(combined_title.lower(), fullscreen)
        return ActiveWindowState(
            window_handle=window_handle,
            process_id=process_id,
            process_name=process_name,
            title=title,
            activity=activity,
            app_source=cls._detect_app_source(combined_title.lower(), activity),
            fullscreen=fullscreen,
            error=error,
        )

    def _record_window_state(
        self,
        state: ActiveWindowState,
    ) -> WindowChangeEvent | None:
        with self._lock:
            previous = self._active_window_state
            self._active_window_state = state
            if state.activity != self._current_activity:
                self._last_activity = self._current_activity
                self._current_activity = state.activity
            if previous is None:
                if state.activity == "idle_desktop":
                    return None
                previous = self._make_window_state("", None, None, "", False)
                kind = "open"
            elif previous.signature == state.signature:
                return None
            elif previous.activity != "idle_desktop" and state.activity == "idle_desktop":
                kind = "close"
            elif previous.process_id != state.process_id:
                kind = "switch"
            elif previous.window_handle != state.window_handle:
                kind = "focus"
            elif previous.title != state.title:
                kind = "title"
            else:
                return None
            return WindowChangeEvent(
                previous=previous,
                current=state,
                kind=kind,
                occurred_at=time.time(),
            )

    @classmethod
    def _detect_activity(cls, title: str) -> str:
        normalized_title = f" {title.strip().lower()} "
        browser_title = any(
            cls._matches_keyword(title, keyword)
            for keyword in cls._BROWSER_KEYWORDS
        )
        if browser_title and any(
            cls._matches_keyword(title, keyword)
            for keyword in cls._SEARCH_KEYWORDS
        ):
            return "web_search"
        for activity, keywords in cls._ACTIVITY_KEYWORDS.items():
            if any(
                cls._matches_keyword(normalized_title, keyword)
                for keyword in keywords
            ):
                return activity
        if not title.strip() or any(
            marker in normalized_title for marker in ("desktop", "program manager")
        ):
            return "idle_desktop"
        return "other"

    @classmethod
    def _activity_for_window(cls, title: str, fullscreen: bool) -> str:
        activity = cls._detect_activity(title)
        if fullscreen and activity == "other":
            return "gaming"
        return activity

    @staticmethod
    def _matches_keyword(title: str, keyword: str) -> bool:
        normalized_keyword = keyword.strip().lower()
        return (
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])",
                title.lower(),
            )
            is not None
        )

    @classmethod
    def _detect_app_source(cls, title: str, activity: str) -> str:
        source_activity = "browsing" if activity == "web_search" else activity
        keywords = cls._ACTIVITY_KEYWORDS.get(source_activity, ())
        for keyword in keywords:
            if cls._matches_keyword(title, keyword):
                return re.sub(r"[^a-z0-9]+", "_", keyword.strip().lower()).strip("_")
        return activity

    @staticmethod
    def _read_linux_active_title() -> str:
        xdotool = shutil.which("xdotool")
        if xdotool is None:
            raise OSError(
                "Install xdotool on X11 to enable active-window titles; "
                "Wayland may restrict this information."
            )
        result = subprocess.run(
            [xdotool, "getactivewindow", "getwindowname"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "xdotool could not read the active window.")
        return result.stdout.strip()

    @staticmethod
    def _read_temperatures() -> tuple[float | None, float | None, str | None]:
        try:
            sensor_groups = psutil.sensors_temperatures(fahrenheit=False)
        except Exception as error:
            return (
                None,
                None,
                f"Temperature telemetry unavailable: {type(error).__name__}: {error}",
            )
        if not sensor_groups:
            return None, None, "Temperature telemetry unavailable: no sensors exposed by this platform."
        all_values: list[float] = []
        cpu_values: list[float] = []
        for group, readings in (sensor_groups or {}).items():
            for reading in readings:
                try:
                    value = float(reading.current)
                except (AttributeError, TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue
                all_values.append(value)
                label = f"{group} {getattr(reading, 'label', '')}".lower()
                if any(marker in label for marker in ("cpu", "core", "package", "k10temp")):
                    cpu_values.append(value)
        return (
            max(cpu_values) if cpu_values else None,
            max(all_values) if all_values else None,
            None,
        )

    def _is_fullscreen(self, window: object | None) -> bool:
        if window is None:
            return False
        try:
            left = int(getattr(window, "left"))
            top = int(getattr(window, "top"))
            width = int(getattr(window, "width"))
            height = int(getattr(window, "height"))
            screen_width, screen_height = self._get_screen_size()
        except (
            AttributeError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
        ):
            return False
        tolerance = 2
        return (
            left <= tolerance
            and top <= tolerance
            and width >= screen_width - tolerance
            and height >= screen_height - tolerance
        )

    def _get_screen_size(self) -> tuple[int, int]:
        if self._screen_size is not None:
            return self._screen_size
        if sys.platform == "win32":
            import ctypes

            self._screen_size = (
                int(ctypes.windll.user32.GetSystemMetrics(0)),
                int(ctypes.windll.user32.GetSystemMetrics(1)),
            )
        else:
            xrandr = shutil.which("xrandr")
            if xrandr is None:
                raise OSError("Screen dimensions are unavailable on this platform.")
            result = subprocess.run(
                [xrandr, "--current"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            match = re.search(r"^\s*(\d+)x(\d+)\+\d+\+\d+.*\*", result.stdout, re.MULTILINE)
            if result.returncode != 0 or match is None:
                raise OSError("Could not determine the active display dimensions.")
            self._screen_size = (int(match.group(1)), int(match.group(2)))
        return self._screen_size

    def get_snapshot(self, refresh: bool = False) -> Telemetry:
        """Return the latest telemetry, optionally sampling synchronously."""
        if refresh:
            return self.sample()
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return self.get_snapshot(refresh=True)
        return snapshot

    @staticmethod
    def _empty_snapshot() -> Telemetry:
        now = time.time()
        return Telemetry(
            cpu_percent=0.0,
            ram_percent=0.0,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=datetime.now().hour,
            late_night=False,
            active_window_title="",
            app_source="other",
            window_changed=False,
            activity="other",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=now,
        )

    def start(
        self,
        callback: Callable[[Telemetry], None] | None = None,
        window_callback: Callable[[WindowChangeEvent], None] | None = None,
    ) -> None:
        """Start background sampling without waiting for hardware or window APIs."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._callback = callback
            self._window_callback = window_callback
            self._snapshot = self._empty_snapshot()
            self._thread = threading.Thread(
                target=self._run,
                name="retro-pet-system-monitor",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        next_window_sample = 0.0
        next_hardware_sample = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_window_sample:
                state = self._read_active_window()
                if state.error:
                    with self._lock:
                        previous = self._active_window_state
                    if previous is not None:
                        state = replace(previous, error=state.error)
                event = self._record_window_state(state)
                with self._lock:
                    snapshot = self._snapshot or self._empty_snapshot()
                    snapshot = replace(
                        snapshot,
                        active_window_title=state.title,
                        app_source=state.app_source,
                        activity=state.activity,
                        last_activity=self._last_activity,
                        window_handle=state.window_handle,
                        process_id=state.process_id,
                        process_name=state.process_name,
                        fullscreen=state.fullscreen,
                        coding_detected=state.activity == "coding",
                        window_changed=event is not None,
                        sampled_at=time.time(),
                    )
                    self._snapshot = snapshot
                    window_callback = self._window_callback
                if event is not None and window_callback is not None:
                    window_callback(event)
                next_window_sample = now + self.window_poll_interval_seconds

            if now >= next_hardware_sample:
                with self._lock:
                    state = self._active_window_state
                    callback = self._callback
                if state is not None:
                    snapshot = self._sample_telemetry(state, window_changed=False)
                    with self._lock:
                        self._snapshot = snapshot
                    if callback is not None:
                        callback(snapshot)
                next_hardware_sample = now + self.interval_seconds

            wait_seconds = min(
                max(0.01, next_window_sample - time.monotonic()),
                max(0.01, next_hardware_sample - time.monotonic()),
            )
            if self._stop_event.wait(wait_seconds):
                break

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the sampler and wait briefly for its worker thread to exit."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("System monitor did not stop within the timeout.")


class WindowsNotificationListener:
    """Watch Windows toast notifications when the OS grants listener access."""

    def __init__(
        self,
        callback: Callable[[SystemNotification], None],
        status_callback: Callable[[str], None] | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Notification polling interval must be greater than zero.")
        self._callback = callback
        self._status_callback = status_callback
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen_ids: set[str] = set()

    @staticmethod
    def _stable_id(notification: object) -> str:
        notification_id = getattr(notification, "id", None)
        if notification_id is None:
            raise ValueError("Windows notification did not provide an ID.")
        app_info = getattr(notification, "app_info", None)
        app_id = getattr(app_info, "app_user_model_id", "") if app_info is not None else ""
        creation_time = getattr(notification, "creation_time", "")
        return f"{app_id}:{notification_id}:{creation_time}"

    @staticmethod
    def _app_name(notification: object) -> str:
        app_info = getattr(notification, "app_info", None)
        if app_info is None:
            return "An app"
        display_info = getattr(app_info, "display_info", None)
        name = getattr(display_info, "display_name", "") if display_info is not None else ""
        return str(name or getattr(app_info, "app_user_model_id", "") or "An app")

    def _to_event(self, notification: object) -> SystemNotification:
        app_name = self._app_name(notification)
        activity = SystemMonitor._detect_activity(app_name.lower())
        source = (
            SystemMonitor._detect_app_source(app_name.lower(), activity)
            if activity != "other"
            else "other"
        )
        return SystemNotification(
            notification_id=self._stable_id(notification),
            app_name=app_name,
            source=source,
        )

    def start(self) -> None:
        """Request access on the caller's UI thread, then watch in a worker."""
        if sys.platform != "win32":
            self._report_status("Windows notification listening is only available on Windows.")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            management = importlib.import_module(
                "winsdk.windows.ui.notifications.management"
            )
            UserNotificationListener = management.UserNotificationListener
            listener = UserNotificationListener.current
            access_operation = listener.request_access_async()
        except (ImportError, OSError, RuntimeError, AttributeError) as error:
            self._report_status(f"Windows notification listener unavailable: {error}")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(listener, access_operation),
            name="retro-pet-windows-notifications",
            daemon=True,
        )
        self._thread.start()

    def _report_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _run(self, listener: object, access_operation: object) -> None:
        try:
            asyncio.run(self._listen(listener, access_operation))
        except Exception as error:
            self._report_status(
                f"Windows notification listener stopped: {type(error).__name__}: {error}"
            )

    async def _listen(self, listener: object, access_operation: object) -> None:
        notifications_module = importlib.import_module(
            "winsdk.windows.ui.notifications"
        )
        management_module = importlib.import_module(
            "winsdk.windows.ui.notifications.management"
        )
        NotificationKinds = notifications_module.NotificationKinds
        UserNotificationListenerAccessStatus = (
            management_module.UserNotificationListenerAccessStatus
        )

        try:
            access_status = await access_operation
        except Exception as error:
            self._report_status(
                "Windows notification permission could not be checked. "
                f"App and window detection will continue: {error}"
            )
            return
        if access_status != UserNotificationListenerAccessStatus.ALLOWED:
            self._report_status(
                "Windows notification access is disabled or was not granted. "
                "Enable notification access in Windows Settings to track toasts."
            )
            return

        loop = asyncio.get_running_loop()
        pending_ids: set[int] = set()
        pending_lock = threading.Lock()
        initializing = True

        async def deliver_notification(notification_id: int) -> None:
            try:
                notification = await listener.get_notification(notification_id)
            except Exception as error:
                self._report_status(
                    "A Windows notification could not be read; app and window "
                    f"detection will continue: {error}"
                )
                return
            if notification is not None:
                self._deliver(notification)

        def notification_changed(_sender: object, args: object) -> None:
            change_kind = str(getattr(args, "change_kind", "")).lower()
            notification_id = getattr(args, "user_notification_id", None)
            if "added" not in change_kind or notification_id is None:
                return
            notification_id = int(notification_id)
            with pending_lock:
                if initializing:
                    pending_ids.add(notification_id)
                    return
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(deliver_notification(notification_id))
            )

        add_handler = getattr(listener, "add_notification_changed", None)
        remove_handler = getattr(listener, "remove_notification_changed", None)
        handler_registered = False
        try:
            if add_handler is not None:
                add_handler(notification_changed)
                handler_registered = True

            existing = await listener.get_notifications_async(NotificationKinds.TOAST)
            for notification in existing:
                self._seen_ids.add(self._stable_id(notification))
            with pending_lock:
                initializing = False
                pending = tuple(pending_ids)
                pending_ids.clear()
            for notification_id in pending:
                await deliver_notification(notification_id)
            self._report_status("Windows notification access is enabled.")

            while not self._stop_event.is_set():
                notifications = await listener.get_notifications_async(NotificationKinds.TOAST)
                for notification in notifications:
                    self._deliver(notification)
                await asyncio.to_thread(
                    self._stop_event.wait,
                    self._poll_interval_seconds,
                )
        finally:
            if handler_registered and remove_handler is not None:
                remove_handler(notification_changed)

    def _deliver(self, notification: object) -> None:
        event = self._to_event(notification)
        if event.notification_id in self._seen_ids:
            return
        self._seen_ids.add(event.notification_id)
        self._callback(event)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the notification polling loop without blocking the UI."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("Windows notification listener did not stop in time.")
