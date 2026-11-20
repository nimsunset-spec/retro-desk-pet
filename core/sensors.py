"""Low-overhead background snapshots of local system telemetry."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
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

    def as_dict(self) -> dict[str, float | int | bool | str | None]:
        """Return a plain dictionary suitable for the brain and UI."""
        return asdict(self)


class SystemMonitor:
    """Poll system telemetry and track active-window activity transitions."""

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

    def __init__(self, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("Monitor interval must be greater than zero.")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: Telemetry | None = None
        self._callback: Callable[[Telemetry], None] | None = None
        self._last_activity: str | None = None
        self._current_activity: str | None = None
        self._current_window_title: str | None = None
        self._screen_size: tuple[int, int] | None = None

    def sample(self) -> Telemetry:
        """Read a fresh hardware and active-window snapshot."""
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
        active_window_title = ""
        active_window = None

        if pygetwindow is not None:
            try:
                active_window_title = pygetwindow.getActiveWindowTitle() or ""
                get_active_window = getattr(pygetwindow, "getActiveWindow", None)
                if get_active_window is not None:
                    active_window = get_active_window()
            except Exception as error:
                if not sys.platform.startswith("linux"):
                    errors.append(f"Window telemetry unavailable: {error}")
        elif _WINDOW_IMPORT_ERROR is not None:
            if not sys.platform.startswith("linux"):
                errors.append(f"Window telemetry unavailable: {_WINDOW_IMPORT_ERROR}")

        if sys.platform.startswith("linux") and not active_window_title:
            try:
                active_window_title = self._read_linux_active_title()
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f"Linux window title unavailable: {error}")

        hour = datetime.now().hour
        title = active_window_title.lower()
        late_night = hour >= 23 or hour < 4
        fullscreen = self._is_fullscreen(active_window)
        activity = self._activity_for_window(title, fullscreen)

        with self._lock:
            normalized_title = " ".join(title.split())
            window_changed = (
                self._current_window_title is not None
                and normalized_title != self._current_window_title
            )
            self._current_window_title = normalized_title
            if activity != self._current_activity:
                self._last_activity = self._current_activity
                self._current_activity = activity
            last_activity = self._last_activity

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
            active_window_title=active_window_title,
            app_source=self._detect_app_source(title, activity),
            window_changed=window_changed,
            activity=activity,
            last_activity=last_activity,
            fullscreen=fullscreen,
            coding_detected=activity == "coding",
            sampled_at=time.time(),
            error="; ".join(errors) if errors else None,
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
            snapshot = self.sample()
            with self._lock:
                self._snapshot = snapshot
            return snapshot
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return self.get_snapshot(refresh=True)
        return snapshot

    def start(self, callback: Callable[[Telemetry], None] | None = None) -> None:
        """Start periodic sampling; calling start twice has no extra effect."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._callback = callback
        snapshot = self.sample()
        with self._lock:
            self._snapshot = snapshot
            self._thread = threading.Thread(
                target=self._run,
                name="retro-pet-system-monitor",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            snapshot = self.sample()
            with self._lock:
                self._snapshot = snapshot
                callback = self._callback
            if callback is not None:
                callback(snapshot)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the sampler and wait briefly for its worker thread to exit."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("System monitor did not stop within the timeout.")
