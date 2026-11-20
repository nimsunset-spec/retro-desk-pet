"""Low-overhead background snapshots of local system telemetry."""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import logging
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable

import psutil

LOGGER = logging.getLogger(__name__)

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

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary suitable for the brain and UI."""
        return asdict(self)


@dataclass(frozen=True)
class ActivitySnapshot(Telemetry):
    """Structured foreground, resource, media, and notification context."""

    detail: str = ""
    extra: str = ""
    mood: str = "bored"
    tab_count: int = 0
    unused_tabs: tuple[dict[str, str | float], ...] = ()
    tab_names: tuple[str, ...] = ()
    top_processes: tuple[dict[str, str | float | int], ...] = ()
    media_title: str = ""
    media_artist: str = ""
    media_state: str = "unknown"
    media_source: str = ""
    unread_notification_count: int = 0
    notification_apps: tuple[str, ...] = ()
    game_duration_seconds: float = 0.0
    active_filename: str = ""
    project_name: str = ""
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    background_processes: tuple[dict[str, str | float | int], ...] = ()
    activity_changed: bool = False

    @property
    def activity_signature(self) -> tuple[str, str, str, str]:
        """Stable content signature independent of a browser's window handle."""
        app_name = self.process_name.replace("/", "\\").rsplit("\\", 1)[-1]
        title = (
            self.media_title or self.detail
            if self.activity in {"spotify", "spotify_browsing"}
            else self.detail
        )
        return (
            " ".join(self.activity.casefold().split()),
            " ".join(title.casefold().split()),
            " ".join(self.media_artist.casefold().split()),
            " ".join((app_name or self.app_source).casefold().split()),
        )


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
    detail: str = ""
    extra: str = ""
    browser_tabs: tuple[str, ...] = ()
    active_filename: str = ""
    project_name: str = ""
    media_title: str = ""
    media_artist: str = ""
    media_state: str = "unknown"

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
        "gaming": (),
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
        "pinterest": ("pinterest",),
        "social_media": (
            "reddit",
            "twitter",
            " x ",
            "instagram",
            "discord",
            "tiktok",
            "facebook",
            "whatsapp",
            "telegram",
        ),
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
        "browsing": (
            "chrome", "chromium", "edge", "msedge", "firefox", "brave",
            "opera", "vivaldi", "zen browser",
        ),
    }
    _BROWSER_KEYWORDS = _ACTIVITY_KEYWORDS["browsing"]
    _SEARCH_KEYWORDS = ("google search", "bing", "duckduckgo", "search results", "startpage")
    _BROWSER_EXECUTABLES = {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "firefox.exe",
        "opera.exe",
        "vivaldi.exe",
        "chromium.exe",
        "zen.exe",
        "zen browser.exe",
    }
    _GAME_EXECUTABLES = {
        "minecraft.exe": "Minecraft",
        "valorant-win64-shipping.exe": "VALORANT",
        "hades.exe": "Hades",
        "celeste.exe": "Celeste",
        "stardew valley.exe": "Stardew Valley",
        "stardewvalley.exe": "Stardew Valley",
        "hollow_knight.exe": "Hollow Knight",
        "terraria.exe": "Terraria",
        "robloxplayerbeta.exe": "Roblox",
        "robloxplayer.exe": "Roblox",
        "overwatch.exe": "Overwatch",
        "eldenring.exe": "Elden Ring",
        "witcher3.exe": "The Witcher 3",
        "cyberpunk2077.exe": "Cyberpunk 2077",
        "rocketleague.exe": "Rocket League",
        "league of legends.exe": "League of Legends",
        "among us.exe": "Among Us",
        "among us": "Among Us",
        "fortniteclient-win64-shipping.exe": "Fortnite",
        "r5apex.exe": "Apex Legends",
        "cs2.exe": "Counter-Strike 2",
        "dota2.exe": "Dota 2",
        "fallguys_client_game.exe": "Fall Guys",
        "genshinimpact.exe": "Genshin Impact",
        "starrail.exe": "Honkai: Star Rail",
    }

    def __init__(
        self,
        interval_seconds: float = 0.5,
        window_poll_interval_seconds: float = 0.5,
    ) -> None:
        if interval_seconds <= 0 or window_poll_interval_seconds <= 0:
            raise ValueError("Monitor intervals must be greater than zero.")
        self.interval_seconds = min(interval_seconds, 0.5)
        self.window_poll_interval_seconds = min(window_poll_interval_seconds, 0.5)
        self._stop_event = threading.Event()
        self._refresh_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: ActivitySnapshot | None = None
        self._callback: Callable[[Telemetry], None] | None = None
        self._window_callback: Callable[[WindowChangeEvent], None] | None = None
        self._last_activity: str | None = None
        self._current_activity: str | None = None
        self._active_window_state: ActiveWindowState | None = None
        self._last_window_change_at = 0.0
        self._last_activity_signature: tuple[str, str, str, str] | None = None
        self._screen_size: tuple[int, int] | None = None
        self._uia_probe = None
        self._tab_last_active: dict[str, float] = {}
        self._game_started_at: float | None = None
        self._process_last_active: dict[int, float] = {}
        self._coding_app_running = False
        self._top_processes: tuple[dict[str, str | float | int], ...] = ()
        self._last_process_audit = 0.0
        self._notification_count = 0
        self._notification_apps: tuple[str, ...] = ()
        self._media: dict[str, str] = {
            "title": "",
            "artist": "",
            "state": "unknown",
            "source": "",
        }
        self._media_tracker: WindowsMediaTracker | None = None

    def sample(self) -> Telemetry:
        """Return the latest cached sample without making sensor calls."""
        return self.get_snapshot(refresh=True)

    def sample_foreground_snapshot(self) -> ActivitySnapshot:
        """Return the cached snapshot; all live reads happen in the monitor thread."""
        return self.get_snapshot(refresh=True)

    @property
    def window_change_age_seconds(self) -> float:
        with self._lock:
            changed_at = self._last_window_change_at
        if changed_at <= 0:
            return float("inf")
        return max(0.0, time.monotonic() - changed_at)

    def _sample_telemetry(
        self,
        state: ActiveWindowState,
        window_changed: bool,
        cached_snapshot: ActivitySnapshot | None = None,
    ) -> ActivitySnapshot:
        errors: list[str] = []
        if cached_snapshot is None:
            cpu_percent = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory()
            ram_percent = memory.percent
            ram_used_gb = float(memory.used) / (1024.0**3)
            ram_total_gb = float(memory.total) / (1024.0**3)
            battery = psutil.sensors_battery()
            battery_percent = None if battery is None else float(battery.percent)
            charging_state = None if battery is None else battery.power_plugged
            is_charging = None if charging_state is None else bool(charging_state)
            battery_plugged_in = is_charging
            cpu_temperature_c, system_temperature_c, temperature_error = self._read_temperatures()
            if temperature_error:
                errors.append(temperature_error)
            top_processes = self._audit_processes(state.process_id, time.monotonic())
        else:
            cpu_percent = cached_snapshot.cpu_percent
            ram_percent = cached_snapshot.ram_percent
            ram_used_gb = cached_snapshot.ram_used_gb
            ram_total_gb = cached_snapshot.ram_total_gb
            battery_percent = cached_snapshot.battery_percent
            battery_plugged_in = cached_snapshot.battery_plugged_in
            is_charging = cached_snapshot.is_charging
            cpu_temperature_c = cached_snapshot.cpu_temperature_c
            system_temperature_c = cached_snapshot.system_temperature_c
            top_processes = cached_snapshot.top_processes
        if state.error:
            errors.append(state.error)
        hour = datetime.now().hour
        late_night = hour >= 23 or hour < 4
        now = time.monotonic()
        unused_tabs = self._track_tabs(state.browser_tabs, state.detail, now)
        with self._lock:
            media = dict(self._media)
            unread_notification_count = self._notification_count
            notification_apps = self._notification_apps
            game_started_at = self._game_started_at
        background_processes = tuple(
            process
            for process in top_processes
            if int(process["pid"]) != state.process_id
        )
        media_source = str(media.get("source", "")).casefold()
        foreground_is_media = (
            state.activity in {"music", "spotify"}
            and (not media_source or "spotify" in media_source)
        )
        foreground_is_youtube = (
            state.activity in {"youtube_video", "youtube_music"}
            and (
                not media_source
                or any(browser in media_source for browser in ("chrome", "edge", "firefox", "brave"))
            )
        )
        reported_media_title = str(media.get("title", ""))
        reported_media_artist = str(media.get("artist", ""))
        reported_media_state = str(media.get("state", "unknown"))
        reported_media_source = str(media.get("source", ""))
        if state.activity == "spotify" and not (
            media["title"] and foreground_is_media
        ):
            reported_media_title = state.media_title
            reported_media_artist = state.media_artist
            reported_media_state = state.media_state
            reported_media_source = "Spotify"
        if reported_media_title and (foreground_is_media or foreground_is_youtube):
            activity = (
                "youtube_music"
                if reported_media_state == "playing" and foreground_is_youtube
                else "spotify"
                if foreground_is_media
                else state.activity
            )
            detail = (
                f"{reported_media_title} - {reported_media_artist}".strip(" -")
                if reported_media_artist
                else reported_media_title
            )
        else:
            activity = state.activity
            detail = state.detail
        if activity == "gaming":
            mood = "annoyed" if game_started_at and now - game_started_at > 7200 else "judging"
        elif cpu_percent > 88 or ram_percent > 90:
            mood = "secretly_caring"
        elif unread_notification_count >= 10:
            mood = "annoyed"
        elif state.activity in {"coding", "terminal"}:
            mood = "flustered"
        elif activity in {"browsing", "social_media", "youtube_video"}:
            mood = "judging"
        else:
            mood = "bored"
        extra_parts = [value for value in (state.extra, reported_media_state) if value]
        if self._coding_app_running:
            extra_parts.append("coding editor remains open")
        if top_processes:
            extra_parts.append(
                f"top resource app: {top_processes[0]['name']} "
                f"{top_processes[0]['memory_mb']:.0f} MB"
            )
        return ActivitySnapshot(
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
            activity=activity,
            last_activity=self._last_activity,
            fullscreen=state.fullscreen,
            coding_detected=activity in {"coding", "terminal"},
            sampled_at=time.time(),
            error="; ".join(errors) if errors else None,
            window_handle=state.window_handle,
            process_id=state.process_id,
            process_name=state.process_name,
            detail=detail,
            extra="; ".join(extra_parts),
            mood=mood,
            tab_count=len(state.browser_tabs),
            unused_tabs=unused_tabs,
            tab_names=state.browser_tabs,
            top_processes=top_processes,
            media_title=reported_media_title,
            media_artist=reported_media_artist,
            media_state=reported_media_state,
            media_source=reported_media_source,
            unread_notification_count=unread_notification_count,
            notification_apps=notification_apps,
            game_duration_seconds=(
                max(0.0, now - game_started_at) if game_started_at is not None else 0.0
            ),
            active_filename=state.active_filename,
            project_name=state.project_name,
            ram_used_gb=ram_used_gb,
            ram_total_gb=ram_total_gb,
            background_processes=background_processes,
        )

    def _track_tabs(
        self,
        tab_names: tuple[str, ...],
        active_detail: str,
        now: float,
    ) -> tuple[dict[str, str | float], ...]:
        active_tab = None
        if tab_names:
            for tab in tab_names:
                self._tab_last_active.setdefault(tab, now)
            active_tab = next(
                (tab for tab in tab_names if tab.casefold() in active_detail.casefold()),
                tab_names[0],
            )
            self._tab_last_active[active_tab] = now
        if len(self._tab_last_active) > 120:
            expired = sorted(
                (
                    (tab, last_active)
                    for tab, last_active in self._tab_last_active.items()
                    if tab != active_tab
                ),
                key=lambda item: item[1],
            )
            for tab, _ in expired[: len(self._tab_last_active) - 120]:
                del self._tab_last_active[tab]
        unused: list[dict[str, str | float]] = []
        for tab in self._tab_last_active:
            if tab == active_tab:
                continue
            last_active = self._tab_last_active[tab]
            unused.append(
                {
                    "title": tab,
                    "idle_seconds": round(max(0.0, now - last_active), 1),
                }
            )
        return tuple(sorted(unused, key=lambda item: float(item["idle_seconds"]), reverse=True))

    def _audit_processes(
        self,
        active_process_id: int | None,
        now: float,
    ) -> tuple[dict[str, str | float | int], ...]:
        if self._last_process_audit and now - self._last_process_audit < 5.0:
            return self._top_processes
        self._last_process_audit = now
        processes: list[dict[str, str | float | int]] = []
        active_ids: set[int] = set()
        coding_app_running = False
        for process in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                pid = int(process.info["pid"])
                name = str(process.info.get("name") or "unknown")
                if name.casefold() in {
                    "code.exe",
                    "pycharm64.exe",
                    "cursor.exe",
                    "devenv.exe",
                    "sublime_text.exe",
                }:
                    coding_app_running = True
                memory_info = process.info.get("memory_info")
                memory_mb = (
                    float(memory_info.rss) / (1024.0 * 1024.0)
                    if memory_info is not None
                    else 0.0
                )
                cpu_percent = float(process.cpu_percent(interval=None))
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
            active_ids.add(pid)
            if cpu_percent > 0.5 or pid == active_process_id:
                self._process_last_active[pid] = now
            last_active = self._process_last_active.setdefault(pid, now)
            processes.append(
                {
                    "pid": pid,
                    "name": name,
                    "memory_mb": round(memory_mb, 1),
                    "cpu_percent": round(cpu_percent, 1),
                    "idle_seconds": round(max(0.0, now - last_active), 1),
                }
            )
        for stale_pid in set(self._process_last_active) - active_ids:
            del self._process_last_active[stale_pid]
        ranked = sorted(
            processes,
            key=lambda item: float(item["memory_mb"]) + float(item["cpu_percent"]) * 2.0,
            reverse=True,
        )
        self._top_processes = tuple(ranked[:8])
        self._coding_app_running = coding_app_running
        return self._top_processes

    def update_notification_summary(self, count: int, apps: tuple[str, ...]) -> None:
        """Publish the current Windows Action Center count to the live snapshot."""
        with self._lock:
            self._notification_count = max(0, int(count))
            self._notification_apps = tuple(apps[:20])
            if self._snapshot is not None:
                self._snapshot = replace(
                    self._snapshot,
                    unread_notification_count=self._notification_count,
                    notification_apps=self._notification_apps,
                )

    def update_media_state(self, media: dict[str, str]) -> None:
        """Update cached system media metadata from the asynchronous WinRT tracker."""
        with self._lock:
            self._media = {
                "title": media.get("title", "")[:180],
                "artist": media.get("artist", "")[:120],
                "state": media.get("state", "unknown"),
                "source": media.get("source", "")[:160],
            }

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
        title = title_buffer.value
        combined_title = f"{process_name} {title}".lower()
        browser_tabs: tuple[str, ...] = ()
        return self._make_window_state(
            title=title,
            window_handle=int(window_handle),
            process_id=process_id_value,
            process_name=process_name,
            fullscreen=fullscreen,
            browser_tabs=browser_tabs,
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
        browser_tabs: tuple[str, ...] = (),
    ) -> ActiveWindowState:
        normalized = " ".join(title.split())
        site_title = cls._strip_title_suffixes(
            re.sub(r"^\(\d+\)\s*", "", normalized),
            {
                "google chrome",
                "chrome",
                "microsoft edge",
                "edge",
                "mozilla firefox",
                "firefox",
                "brave",
                "opera",
                "vivaldi",
                "chromium",
                "zen browser",
            },
        )
        process_basename = process_name.replace("/", "\\").rsplit("\\", 1)[-1].casefold()
        cleaned_title = cls._clean_window_title(normalized)
        combined_title = f"{process_name} {normalized}".strip()
        spotify_title = ""
        spotify_artist = ""
        spotify_state = "unknown"
        if process_basename == "explorer.exe" and (
            not normalized
            or normalized.casefold() in {"program manager", "desktop", "windows spotlight"}
            or "desktop" in normalized.casefold()
            or "wallpaper" in normalized.casefold()
        ):
            activity = "idle_desktop"
            cleaned_title = ""
        elif process_basename in cls._GAME_EXECUTABLES:
            activity = "gaming"
            if not cleaned_title or cleaned_title.casefold() in {
                process_basename.removesuffix(".exe"),
                process_name.casefold(),
            }:
                cleaned_title = cls._GAME_EXECUTABLES[process_basename]
        elif process_basename == "spotify.exe":
            activity = "spotify"
            if cleaned_title.casefold() in {"", "spotify", "spotify free", "spotify premium"}:
                spotify_state = "paused"
                cleaned_title = ""
            elif " - " in cleaned_title:
                spotify_artist, spotify_title = cleaned_title.split(" - ", 1)
                spotify_artist = spotify_artist.strip()
                spotify_title = spotify_title.strip()
                cleaned_title = spotify_title
                spotify_state = "playing"
            else:
                spotify_title = cleaned_title
        elif process_basename in cls._BROWSER_EXECUTABLES:
            lowered_title = site_title.casefold()
            if any(
                cls._matches_keyword(lowered_title, marker)
                for marker in ("chatgpt", "claude", "gemini")
            ):
                activity = "ai_chat"
            elif "youtube" in lowered_title:
                music_markers = (
                    "mv",
                    "official video",
                    "lyrics",
                    "lyric video",
                    "official music",
                    "official audio",
                    "audio",
                    "bgm",
                    "ost",
                    "lo-fi",
                    "playlist",
                )
                activity = (
                    "youtube_music"
                    if any(
                        cls._matches_keyword(lowered_title, marker)
                        for marker in music_markers
                    )
                    else "youtube_video"
                )
            elif "pinterest" in lowered_title:
                activity = "pinterest"
                pin_title = re.sub(
                    r"(?:\s[-—|]\s*)?pinterest\s*$",
                    "",
                    site_title,
                    flags=re.IGNORECASE,
                ).strip(" -—|")
                if pin_title.casefold() in {"", "home", "home feed", "pinterest"} or (
                    pin_title.casefold().startswith("pinterest - ")
                    and pin_title.rsplit(" - ", 1)[-1].casefold() in {"home", "home feed"}
                ):
                    cleaned_title = "Pinterest home feed"
                else:
                    cleaned_title = cls._clean_window_title(pin_title) or "Pinterest home feed"
            elif any(
                cls._matches_keyword(lowered_title, marker)
                for marker in (
                    "reddit",
                    "twitter",
                    "x",
                    "instagram",
                    "discord",
                    "tiktok",
                    "facebook",
                    "whatsapp",
                    "telegram",
                )
            ):
                activity = "social_media"
            elif any(
                cls._matches_keyword(lowered_title, keyword)
                for keyword in cls._SEARCH_KEYWORDS
            ):
                activity = "web_search"
            else:
                activity = "browsing"
        else:
            activity = cls._activity_for_window(
                combined_title.lower(),
                fullscreen,
                process_name=process_name,
            )
        detail = cleaned_title
        active_filename = ""
        project_name = ""
        if activity == "coding":
            coding_title = cls._clean_window_title(normalized)
            parts = [
                part.strip()
                for part in re.split(r"\s[-|—]\s", coding_title)
                if part.strip()
            ]
            if parts:
                if len(parts) > 1 and re.search(r"\.[a-z0-9]{1,8}$", parts[-1], re.IGNORECASE):
                    active_filename = parts[-1]
                    project_name = parts[-2]
                else:
                    active_filename = parts[0]
                    project_name = parts[1] if len(parts) > 1 else ""
            detail = f"{active_filename} in {project_name}" if project_name else active_filename or cleaned_title
        elif activity == "pinterest":
            detail = cleaned_title or "Pinterest home feed"

        if activity == "spotify":
            detail = cleaned_title or "Spotify"

        if activity in {"youtube_video", "youtube_music"}:
            detail = cleaned_title or (
                "YouTube music" if activity == "youtube_music" else "YouTube video"
            )
        if activity == "gaming" and not detail:
            detail = cls._GAME_EXECUTABLES.get(process_basename, "the current game")
        if activity in {"idle_desktop", "web_search", "browsing"} and not detail:
            detail = "Desktop" if activity == "idle_desktop" else activity.replace("_", " ")
        detail = cls._word_boundary_limit(detail, 45)
        active_filename = cls._word_boundary_limit(active_filename, 45)
        project_name = cls._word_boundary_limit(project_name, 45)
        return ActiveWindowState(
            window_handle=window_handle,
            process_id=process_id,
            process_name=process_name,
            title=title,
            activity=activity,
            app_source=cls._detect_app_source(
                f"{process_name} {normalized}".lower(),
                activity,
            ),
            fullscreen=fullscreen,
            error=error,
            detail=detail,
            extra=f"tabs={len(browser_tabs)}" if browser_tabs else "",
            browser_tabs=browser_tabs,
            active_filename=active_filename[:120],
            project_name=project_name[:120],
            media_title=spotify_title,
            media_artist=spotify_artist,
            media_state=spotify_state,
        )

    @staticmethod
    def _clean_window_title(title: str) -> str:
        suffixes = {
            "google chrome",
            "chrome",
            "microsoft edge",
            "edge",
            "mozilla firefox",
            "firefox",
            "brave",
            "opera",
            "vivaldi",
            "chromium",
            "zen browser",
            "youtube",
            "spotify",
            "spotify free",
            "spotify premium",
            "pinterest",
            "visual studio code",
            "vs code",
            "pycharm",
            "cursor",
            "visual studio",
        }
        cleaned = re.sub(r"^\(\d+\)\s*", "", title.strip())
        return SystemMonitor._strip_title_suffixes(cleaned, suffixes).strip(" -|—")

    @staticmethod
    def _strip_title_suffixes(title: str, suffixes: set[str]) -> str:
        cleaned = title.strip()
        while True:
            match = re.search(r"\s+(?:-|—|\|)\s+([^|—-]+)$", cleaned)
            if match is None or match.group(1).strip().casefold() not in suffixes:
                return cleaned
            cleaned = cleaned[:match.start()].strip()

    @staticmethod
    def _word_boundary_limit(text: str, limit: int = 45) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        clipped = normalized[: limit + 1]
        boundary = clipped.rfind(" ")
        if boundary <= 0:
            return normalized
        return clipped[:boundary].rstrip(" -|")

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
            if state.activity == "gaming":
                if self._game_started_at is None:
                    self._game_started_at = time.monotonic()
            else:
                self._game_started_at = None
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
            self._last_window_change_at = time.monotonic()
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
    def _activity_for_window(
        cls,
        title: str,
        fullscreen: bool,
        process_name: str = "",
    ) -> str:
        executable = process_name.replace("/", "\\").rsplit("\\", 1)[-1].casefold()
        if executable in cls._GAME_EXECUTABLES:
            return "gaming"
        activity = cls._detect_activity(title)
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

    def get_snapshot(self, refresh: bool = False) -> ActivitySnapshot:
        """Return the latest background snapshot without waiting for sensor APIs."""
        if refresh:
            self._refresh_requested.set()
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._empty_snapshot()
                self._last_activity_signature = self._snapshot.activity_signature
            return self._snapshot

    @staticmethod
    def _empty_snapshot() -> ActivitySnapshot:
        now = time.time()
        hour = datetime.now().hour
        return ActivitySnapshot(
            cpu_percent=0.0,
            ram_percent=0.0,
            battery_percent=None,
            battery_plugged_in=None,
            is_charging=None,
            cpu_temperature_c=None,
            system_temperature_c=None,
            hour=hour,
            late_night=hour >= 23 or hour < 4,
            active_window_title="",
            app_source="other",
            window_changed=False,
            activity="other",
            last_activity=None,
            fullscreen=False,
            coding_detected=False,
            sampled_at=now,
        )

    @property
    def latest_snapshot(self) -> ActivitySnapshot:
        """Thread-safe cached telemetry produced by the background collector."""
        return self.get_snapshot()

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
            empty_snapshot = self._empty_snapshot()
            self._snapshot = empty_snapshot
            self._last_activity_signature = empty_snapshot.activity_signature
            self._thread = threading.Thread(
                target=self._run,
                name="retro-pet-system-monitor",
                daemon=True,
            )
            self._thread.start()
            if sys.platform == "win32":
                self._media_tracker = WindowsMediaTracker(self.update_media_state)
                self._media_tracker.start()

    def _run(self) -> None:
        next_window_sample = 0.0
        next_hardware_sample = 0.0
        while not self._stop_event.is_set():
            try:
                if self._refresh_requested.is_set():
                    self._refresh_requested.clear()
                    next_window_sample = 0.0
                    next_hardware_sample = 0.0
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
                            detail=state.detail,
                            extra=state.extra,
                            tab_count=len(state.browser_tabs),
                            tab_names=state.browser_tabs,
                            active_filename=state.active_filename,
                            project_name=state.project_name,
                            last_activity=self._last_activity,
                            window_handle=state.window_handle,
                            process_id=state.process_id,
                            process_name=state.process_name,
                            fullscreen=state.fullscreen,
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
                            signature = snapshot.activity_signature
                            activity_changed = signature != self._last_activity_signature
                            self._last_activity_signature = signature
                            snapshot = replace(
                                snapshot,
                                activity_changed=activity_changed,
                            )
                            self._snapshot = snapshot
                        if callback is not None:
                            callback(snapshot)
                    next_hardware_sample = now + self.interval_seconds
            except Exception as error:
                message = f"Sensor poll failed: {type(error).__name__}: {error}"
                LOGGER.exception(message)
                with self._lock:
                    current = self._snapshot or self._empty_snapshot()
                    self._snapshot = replace(current, error=message, sampled_at=time.time())
                next_window_sample = time.monotonic() + self.window_poll_interval_seconds
                next_hardware_sample = time.monotonic() + self.interval_seconds

            wait_seconds = min(
                max(0.01, next_window_sample - time.monotonic()),
                max(0.01, next_hardware_sample - time.monotonic()),
            )
            self._stop_event.wait(wait_seconds)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the sampler and wait briefly for its worker thread to exit."""
        self._stop_event.set()
        media_tracker = self._media_tracker
        if media_tracker is not None:
            media_tracker.stop(timeout)
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("System monitor did not stop within the timeout.")


class WindowsMediaTracker:
    """Poll Windows Global System Media Transport Controls without blocking the UI."""

    def __init__(
        self,
        callback: Callable[[dict[str, str]], None],
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Media polling interval must be greater than zero.")
        self._callback = callback
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32" or (
            self._thread is not None and self._thread.is_alive()
        ):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="retro-pet-windows-media",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._poll())
        except Exception as error:
            self._callback(
                {
                    "title": "",
                    "artist": "",
                    "state": f"unavailable:{type(error).__name__}: {error}",
                }
            )

    async def _poll(self) -> None:
        media_module = importlib.import_module("winsdk.windows.media.control")
        manager_type = media_module.GlobalSystemMediaTransportControlsSessionManager
        manager = await asyncio.wait_for(
            manager_type.request_async(),
            timeout=0.5,
        )
        while not self._stop_event.is_set():
            media = {"title": "", "artist": "", "state": "paused", "source": ""}
            sessions = manager.get_sessions()
            for session in sessions:
                try:
                    playback = session.get_playback_info()
                    state = str(getattr(playback.playback_status, "name", playback.playback_status))
                    state = state.rsplit(".", 1)[-1].lower()
                    properties = await asyncio.wait_for(
                        session.try_get_media_properties_async(),
                        timeout=0.5,
                    )
                    title = str(getattr(properties, "title", "") or "").strip()
                    artist = str(getattr(properties, "artist", "") or "").strip()
                except (OSError, RuntimeError, AttributeError, asyncio.TimeoutError):
                    continue
                if title or state == "playing":
                    media = {
                        "title": title[:180],
                        "artist": artist[:120],
                        "state": "playing" if state == "playing" else "paused",
                        "source": str(
                            getattr(session, "source_app_user_model_id", "") or ""
                        )[:160],
                    }
                    if state == "playing":
                        break
            self._callback(media)
            await asyncio.to_thread(self._stop_event.wait, self._poll_interval_seconds)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("Windows media tracker did not stop in time.")


class WindowsNotificationListener:
    """Watch Windows toast notifications when the OS grants listener access."""

    def __init__(
        self,
        callback: Callable[[SystemNotification], None],
        status_callback: Callable[[str], None] | None = None,
        summary_callback: Callable[[int, tuple[str, ...]], None] | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Notification polling interval must be greater than zero.")
        self._callback = callback
        self._status_callback = status_callback
        self._summary_callback = summary_callback
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
        """Start the notification listener without touching WinRT on the caller thread."""
        if sys.platform != "win32":
            self._report_status("Windows notification listening is only available on Windows.")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="retro-pet-windows-notifications",
            daemon=True,
        )
        self._thread.start()

    def _report_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _run(self) -> None:
        try:
            asyncio.run(self._listen())
        except Exception as error:
            self._report_status(
                f"Windows notification listener stopped: {type(error).__name__}: {error}"
            )

    async def _listen(self) -> None:
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
        listener = management_module.UserNotificationListener.current

        try:
            access_status = await listener.request_access_async()
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
            self._publish_summary(existing)
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
                self._publish_summary(notifications)
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

    def _publish_summary(self, notifications: Iterable[object]) -> None:
        if self._summary_callback is None:
            return
        notification_list = tuple(notifications)
        apps = tuple(
            dict.fromkeys(
                self._app_name(notification)
                for notification in notification_list
            )
        )
        self._summary_callback(len(notification_list), apps[:20])

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the notification polling loop without blocking the UI."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("Windows notification listener did not stop in time.")
