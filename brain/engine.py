"""Offline intent matching and composed, grammatically complete pet dialogue."""

from __future__ import annotations

import json
import math
import random
import re
from collections import deque
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from brain.dialogue import (
    ACTIVITY_OBSERVATIONS,
    MOOD_CLOSERS,
    MOOD_OPENERS,
)
from brain.markov import (
    TrigramScorer,
    build_ngram_model,
    collect_dataset_texts,
    load_ngram_model,
)
from core.resources import resource_path

if TYPE_CHECKING:
    from core.memory import PetMemory
    from core.sensors import ActivitySnapshot

TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
FALLBACK_CONFIDENCE_THRESHOLD = 0.30
UNKNOWN_RESPONSE = (
    "I am not sure what you mean yet. Try asking about my stats or your PC, "
    "tell me how you feel, or mention an app you have open."
)

MOOD_ALIASES = {
    "annoyed": "annoyed",
    "flustered": "flustered",
    "judging": "smug",
    "smug": "smug",
    "secretly_caring": "sleepy",
    "sleepy": "sleepy",
    "bored": "bored",
}
MOOD_SPRITES = {
    "annoyed": "sassy",
    "flustered": "happy",
    "smug": "sassy",
    "sleepy": "sleepy",
    "bored": "idle",
}
ACTIVITY_ALIASES = {
    "music": "spotify",
    "browsing": "social_media",
    "coding_slacking": "coding",
    "resource_audit": "high_ram",
    "notifications": "stacked_notifications",
    "idle": "idle_desktop",
}


def _tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.casefold())


def _word_boundary_limit(text: str, limit: int = 45) -> str:
    normalized = " ".join(text.split()).strip(" -|")
    if len(normalized) <= limit:
        return normalized
    boundary = normalized.rfind(" ", 0, limit + 1)
    return normalized[:boundary].rstrip(" -|") if boundary > 0 else normalized


class BrainEngine:
    """Use a tiny local trigram scorer to select from safe response templates."""

    _INTENT_EMOTIONS = {
        "greeting": "playful",
        "praise": "proud",
        "scold": "sheepish",
        "feed": "delighted",
        "play": "delighted",
        "vent_frustration": "supportive",
        "ask_status": "curious",
        "goodbye": "wistful",
    }

    def __init__(self, seed: int | None = None) -> None:
        with resource_path("brain", "dataset.json").open("r", encoding="utf-8") as file:
            self.dataset: dict[str, Any] = json.load(file)
        self.intents = list(self.dataset["intents"])
        self._random = random.Random(seed)
        self._response_history: dict[str, deque[str]] = {}
        self._backend_error: str | None = None
        model_path = resource_path("brain", "ngram_model.json")
        model = load_ngram_model(model_path)
        if model is None:
            model = build_ngram_model(collect_dataset_texts(self.dataset))
        self._scorer = TrigramScorer(model)
        self._examples_by_intent = {
            intent: [
                set(_tokenize(example))
                for example in content.get("examples", ())
                if _tokenize(example)
            ]
            for intent, content in self.dataset["intents"].items()
        }

    @property
    def backend(self) -> str:
        return "templates+trigram"

    @property
    def backend_error(self) -> str | None:
        return self._backend_error

    def _choose_response(self, category: str, responses: list[str]) -> str:
        if not responses:
            raise ValueError(f"No response templates configured for {category!r}.")
        history = self._response_history.setdefault(category, deque(maxlen=12))
        available = [response for response in responses if response not in history]
        selected = self._random.choice(available or responses)
        history.append(selected)
        return selected

    def _examples(self) -> list[tuple[str, str]]:
        augmentation = self.dataset.get("pattern_augmentation", {})
        prefixes = augmentation.get("prefixes", ())
        suffixes = augmentation.get("suffixes", ())
        examples: list[tuple[str, str]] = []
        for intent, content in self.dataset["intents"].items():
            for text in content["examples"]:
                examples.append((text, intent))
                examples.extend((f"{prefix}{text}", intent) for prefix in prefixes)
                examples.extend((f"{text}{suffix}", intent) for suffix in suffixes)
        return examples

    @staticmethod
    def _is_similar_response(candidate: str, recent_lines: list[str]) -> bool:
        candidate_words = set(_tokenize(candidate))
        for recent in recent_lines:
            if candidate.casefold().strip() == recent.casefold().strip():
                return True
            recent_words = set(_tokenize(recent))
            union = candidate_words | recent_words
            overlap = len(candidate_words & recent_words) / len(union) if union else 0.0
            if overlap >= 0.82 or SequenceMatcher(
                None,
                candidate.casefold(),
                recent.casefold(),
            ).ratio() >= 0.88:
                return True
        return False

    def _select_composed_response_parts(
        self,
        activity: str,
        values: dict[str, Any],
        mood: str,
        recent_lines: list[str],
        recent_openers: list[str] | None = None,
        recent_closers: list[str] | None = None,
    ) -> tuple[str, str, str]:
        activity = activity if activity in ACTIVITY_OBSERVATIONS else "user_chat"
        mood = mood if mood in MOOD_OPENERS else "bored"
        available_openers = [
            phrase
            for phrase in MOOD_OPENERS[mood]
            if phrase not in (recent_openers or ())
        ] or list(MOOD_OPENERS[mood])
        available_closers = [
            phrase
            for phrase in MOOD_CLOSERS[mood]
            if phrase not in (recent_closers or ())
        ] or list(MOOD_CLOSERS[mood])
        observations = ACTIVITY_OBSERVATIONS[activity]
        candidates: set[tuple[str, str, str]] = set()
        for _ in range(32):
            observation = self._random.choice(observations).format(
                title=values.get("title") or "the current activity",
                artist=values.get("artist") or "an artist",
                app=values.get("app") or "the foreground app",
                ram_gb=values.get("ram_gb") or "an unknown amount",
                unread_count=values.get("unread_count", 0),
            )
            candidates.add(
                (
                    self._random.choice(available_openers),
                    observation,
                    self._random.choice(available_closers),
                )
            )
        lines_by_text = {
            f"{opener} {observation} {closer}": (opener, closer)
            for opener, observation, closer in candidates
        }
        novel = [
            line for line in lines_by_text
            if not self._is_similar_response(line, recent_lines)
        ]
        pool = novel or [
            line for line in lines_by_text
            if line.casefold().strip() not in {recent.casefold().strip() for recent in recent_lines}
        ] or list(lines_by_text)
        ranked = sorted(
            pool,
            key=lambda line: self._scorer.score(line) + self._random.random() * 0.025,
            reverse=True,
        )
        selected = self._choose_response(f"composed:{activity}:{mood}", ranked[:12])
        opener, closer = lines_by_text[selected]
        return selected, opener, closer

    def _select_composed_response(
        self,
        activity: str,
        values: dict[str, Any],
        mood: str,
        recent_lines: list[str],
    ) -> str:
        selected, _, _ = self._select_composed_response_parts(
            activity,
            values,
            mood,
            recent_lines,
        )
        return selected

    def _mood(self, pet_stats: dict[str, Any] | None, telemetry: dict[str, Any] | None) -> str:
        stats = pet_stats or {}
        sensors = telemetry or {}
        if (
            float(stats.get("happiness", 80.0)) < 30.0
            or float(stats.get("energy", 80.0)) < 25.0
        ):
            return "sleepy"
        requested = str(sensors.get("mood", ""))
        if requested in MOOD_ALIASES:
            return MOOD_ALIASES[requested]
        if float(sensors.get("ram_percent", 0.0)) >= 88.0:
            return "sleepy"
        if bool(sensors.get("late_night", False)):
            return "sleepy"
        return "smug"

    @staticmethod
    def _display_app(values: dict[str, Any]) -> str:
        activity = str(values.get("activity", ""))
        source = str(values.get("app_source", "")).casefold().replace("_", " ")
        social_apps = {
            "reddit": "Reddit",
            "twitter": "X",
            "x": "X",
            "instagram": "Instagram",
            "discord": "Discord",
            "tiktok": "TikTok",
            "facebook": "Facebook",
            "whatsapp": "WhatsApp",
            "telegram": "Telegram",
        }
        ai_apps = {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "gemini": "Gemini",
            "copilot": "Copilot",
            "perplexity": "Perplexity",
        }
        if activity == "social_media" and source in social_apps:
            return social_apps[source]
        if activity == "ai_chat" and source in ai_apps:
            return ai_apps[source]
        process = str(values.get("process_name", "")).replace("\\", "/").rsplit("/", 1)[-1]
        normalized = process.casefold().removesuffix(".exe")
        names = {
            "chrome": "Google Chrome",
            "msedge": "Microsoft Edge",
            "brave": "Brave",
            "firefox": "Firefox",
            "code": "Visual Studio Code",
            "spotify": "Spotify",
            "explorer": "File Explorer",
        }
        if normalized in names:
            return names[normalized]
        source = str(values.get("app_source", "")).replace("_", " ").strip()
        return source.title() if source else "the foreground app"

    @staticmethod
    def _safe_detail(values: dict[str, Any]) -> str:
        activity = str(values.get("activity", ""))
        media_source = str(values.get("media_source", "")).casefold()
        media_matches_foreground = (
            activity in {"spotify", "music"}
            and (not media_source or "spotify" in media_source)
        ) or (
            activity == "youtube_music"
            and (
                not media_source
                or any(name in media_source for name in ("chrome", "edge", "firefox", "brave"))
            )
        )
        detail = str(
            (values.get("media_title") if media_matches_foreground else "")
            or values.get("detail")
            or values.get("active_filename")
            or values.get("active_window_title")
            or ""
        )
        for suffix in (
            "Google Chrome",
            "Microsoft Edge",
            "Mozilla Firefox",
            "Visual Studio Code",
            "YouTube",
            "Spotify",
            "Pinterest",
        ):
            delimiter = f" - {suffix}"
            if delimiter.casefold() in detail.casefold():
                detail = detail.rsplit(delimiter, 1)[0]
        if " in " in detail and values.get("activity") == "coding":
            filename, project = detail.split(" in ", 1)
            detail = f"{filename} in {project}"
        return _word_boundary_limit(detail, 45) or "the current activity"

    def context_reaction(
        self,
        snapshot: dict[str, Any] | ActivitySnapshot,
        memory: PetMemory | None = None,
    ) -> dict[str, str] | None:
        """Create a context-aware response using only the actual foreground snapshot."""
        values = snapshot if isinstance(snapshot, dict) else snapshot.as_dict()
        activity = str(values.get("activity", "other"))
        if activity == "other":
            return None

        unread = int(values.get("unread_notification_count", 0) or 0)
        ram_percent = float(values.get("ram_percent", 0.0) or 0.0)
        if unread >= 4:
            activity = "stacked_notifications"
        elif ram_percent >= 88:
            activity = "high_ram"
        else:
            if (
                activity == "spotify"
                and str(values.get("media_state", "")).casefold() == "paused"
                and not values.get("media_title")
            ):
                activity = "spotify_browsing"
            activity = ACTIVITY_ALIASES.get(activity, activity)
        if activity not in {
            "youtube_music",
            "youtube_video",
            "spotify",
            "spotify_browsing",
            "ai_chat",
            "pinterest",
            "social_media",
            "coding",
            "gaming",
            "idle_desktop",
            "high_ram",
            "stacked_notifications",
        }:
            return None

        mood = self._mood(None, values)
        detail = self._safe_detail(values)
        backgrounds = values.get("background_processes") or values.get("top_processes") or ()
        leading_app = ""
        if backgrounds:
            leading_app = str(backgrounds[0].get("name", "")).replace(".exe", "")
        composed_values = {
            "title": detail,
            "artist": str(values.get("media_artist") or "an artist"),
            "app": self._display_app(values) if activity != "high_ram" else leading_app or "background apps",
            "ram_gb": f"{float(values.get('ram_used_gb', 0.0) or 0.0):.1f}",
            "unread_count": unread,
        }
        recent_lines = memory.recent_spoken_lines(limit=30) if memory else []
        recent_openers = (
            memory.recent_response_phrases("opener", limit=10)
            if memory is not None
            else []
        )
        recent_closers = (
            memory.recent_response_phrases("closer", limit=10)
            if memory is not None
            else []
        )
        message, opener, closer = self._select_composed_response_parts(
            activity,
            composed_values,
            mood,
            recent_lines,
            recent_openers,
            recent_closers,
        )
        recent_sessions = (
            memory.recent_activity_switches().get(activity, 0)
            if memory is not None
            else 0
        )
        if recent_sessions >= 4 and activity in {
            "youtube_music",
            "youtube_video",
            "social_media",
        }:
            message += (
                f" This makes detour number {recent_sessions}; "
                "I am absolutely not keeping score."
            )
        return {
            "sprite": MOOD_SPRITES[mood],
            "mood": mood,
            "message": message,
            "opener": opener,
            "closer": closer,
        }

    def _emotion_response(self, emotion: str) -> dict[str, str]:
        responses = self.dataset.get("emotion_responses", {}).get(emotion)
        if responses is None:
            raise ValueError(f"No response profile configured for emotion {emotion!r}.")
        return {
            "sprite": responses["sprite"],
            "line": self._choose_response(f"emotion:{emotion}", responses["lines"]),
        }

    @staticmethod
    def _is_system_vitals_query(text: str) -> bool:
        tokens = set(_tokenize(text))
        return bool(
            tokens
            & {
                "pc",
                "computer",
                "system",
                "hardware",
                "cpu",
                "processor",
                "ram",
                "memory",
                "temperature",
                "temp",
                "battery",
            }
        )

    @staticmethod
    def _telemetry_number(telemetry: dict[str, Any], key: str) -> float | None:
        value = telemetry.get(key)
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _system_vitals_response(self, telemetry: dict[str, Any] | None) -> str:
        sensors = telemetry or {}
        cpu = self._telemetry_number(sensors, "cpu_percent")
        ram = self._telemetry_number(sensors, "ram_percent")
        if cpu is None or ram is None:
            return "I couldn't read the live CPU and RAM sensors just now. They may be unavailable on this system."

        message = f"CPU at {cpu:.0f}%, RAM at {ram:.0f}%"
        ram_used = self._telemetry_number(sensors, "ram_used_gb")
        if ram_used is not None:
            message += f" ({ram_used:.1f} GB used)"
        message += "."
        battery = self._telemetry_number(sensors, "battery_percent")
        if battery is not None:
            power_state = "charging" if sensors.get("is_charging") else "on battery"
            message += f" Battery at {battery:.0f}% ({power_state})."
        return message

    def activity_reaction(
        self,
        activity: str,
        late_night: bool = False,
    ) -> dict[str, str] | None:
        reaction_key = "coding_late_night" if activity == "coding" and late_night else activity
        reaction = self.dataset.get("activity_reactions", {}).get(reaction_key)
        if reaction is None:
            return None
        mood = "sleepy" if late_night else "smug"
        lines = reaction.get("lines") or [reaction["message"]]
        message = self._choose_response(f"activity:{reaction_key}", lines)
        return {"sprite": MOOD_SPRITES[mood], "mood": mood, "message": message}

    def notification_reaction(self, app_name: str) -> dict[str, str]:
        reaction = self.dataset["notification_reactions"]
        safe_name = _word_boundary_limit(app_name, 45) or "an app"
        line = self._choose_response("notification", reaction["lines"])
        return {
            "sprite": "sassy",
            "mood": "annoyed",
            "message": line.format(app=safe_name),
        }

    def hardware_reaction(
        self,
        event: str,
        telemetry: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        reaction = self.dataset.get("hardware_reactions", {}).get(event)
        if reaction is None:
            return None
        values = telemetry or {}
        value_key = {
            "high_cpu": "cpu_percent",
            "high_ram": "ram_percent",
            "hot_temperature": "cpu_temperature_c",
        }.get(event)
        value = None if value_key is None else self._telemetry_number(values, value_key)
        if event == "hot_temperature" and value is None:
            value = self._telemetry_number(values, "system_temperature_c")
        if value is None:
            value = 0.0
        selected = self._choose_response(f"hardware:{event}", reaction["lines"])
        message = selected.format(value=value)
        if value_key is not None and "{value:" not in selected:
            metric = "temperature" if event == "hot_temperature" else (
                "CPU" if event == "high_cpu" else "RAM"
            )
            message += f" {metric} is at {value:.0f}{'°C' if event == 'hot_temperature' else '%' }."
        mood = "sleepy" if event == "hot_temperature" else "annoyed"
        return {"sprite": MOOD_SPRITES[mood], "mood": mood, "message": message}

    def idle_daydream(self) -> dict[str, str]:
        reaction = self.hardware_reaction("idle_thought")
        if reaction is None:
            raise RuntimeError("The idle-thought response bank is missing.")
        return reaction

    def ambient_reaction(
        self,
        snapshot: dict[str, Any] | ActivitySnapshot,
        memory: PetMemory | None = None,
    ) -> dict[str, str] | None:
        reaction = self.context_reaction(snapshot, memory)
        if reaction is not None:
            return reaction
        return self.idle_daydream()

    def action_reaction(self, action: str) -> dict[str, str]:
        responses = {
            "feed": (
                "A snack? I suppose you do have your moments. Thanks!",
                "Yum. Don't look so pleased with yourself; I was hungry.",
                "You fed me! I was absolutely not going to ask. Hmph.",
            ),
            "pet": (
                "That was nice. I will allow exactly one more pat.",
                "H-hey, that's actually kind of nice. Don't get used to it.",
                "Friendship points acquired. I was keeping score, obviously.",
            ),
            "play": (
                "Game on! Try to keep up with my superior pixel reflexes.",
                "Playing with me? Fine, but I am choosing the game.",
                "That was fun. I mean, adequate. My energy bar disagrees.",
            ),
        }
        message = self._choose_response(f"manual_action:{action}", list(responses[action]))
        mood = "flustered" if action == "pet" else "smug"
        return {
            "sprite": MOOD_SPRITES[mood],
            "mood": mood,
            "message": message,
        }

    def stats_reaction(self, stats: dict[str, Any]) -> dict[str, str]:
        closer = self._choose_response(
            "stats",
            [
                "There. Your report. Try to look impressed.",
                "I compiled that for you. You are welcome, obviously.",
                "The numbers are fine. I checked twice, not because I care.",
            ],
        )
        message = (
            f"{stats['name']}  LV {stats['level']}\n"
            f"Hunger {stats['hunger']:.0f}%  Energy {stats['energy']:.0f}%\n"
            f"Happiness {stats['happiness']:.0f}%  XP {stats['friendship_xp']}\n"
            f"{closer}"
        )
        return {
            "sprite": MOOD_SPRITES["smug"],
            "mood": "judging",
            "message": message,
        }

    def poke_reaction(
        self,
        snapshot: dict[str, Any] | ActivitySnapshot,
        memory: PetMemory | None = None,
    ) -> dict[str, str]:
        values = snapshot if isinstance(snapshot, dict) else snapshot.as_dict()
        activity = self._safe_detail(values)
        recent_lines = memory.recent_spoken_lines(limit=30) if memory else []
        templates = [
            "You poked me while you're focused on {activity}. Rude. I was monitoring it professionally.",
            "Hey! I can see you're busy with {activity}. Not that I wanted your attention or anything.",
            "A poke? While {activity} is happening? Fine, you have my attention for exactly one pixel-second.",
            "I noticed that. You're doing {activity}, and apparently bothering me is also on the schedule.",
            "Don't poke me while I'm observing {activity}. ...Unless you have something to say.",
            "Busy with {activity}, are we? I was not waiting for a click. Obviously.",
            "That was a poke. I was watching you handle {activity}, not waiting around. Hmph.",
            "I see {activity} has competition for your attention. It is me. Don't make this weird.",
        ]
        available = [
            line for line in templates
            if not self._is_similar_response(line.format(activity=activity), recent_lines)
        ]
        template = self._choose_response("poke", available or templates)
        return {
            "sprite": "sassy",
            "mood": "annoyed",
            "message": template.format(activity=activity),
        }

    def notification_count_reaction(
        self,
        unread_count: int,
        memory: PetMemory | None = None,
    ) -> dict[str, str]:
        templates = [
            "You have {count} unread notifications stacked up. I am not judging. Much.",
            "{count} notifications are waiting for you. Your little red badges are multiplying.",
            "The Action Center says {count} unread. I can ignore them for you. Kidding. Mostly.",
            "There are {count} unread notifications now. Your inbox is getting clingy.",
            "{count} unread pings. Should I organize them, or would that be too helpful?",
            "Notification count: {count}. I noticed before you did. Don't make a big deal of it.",
        ]
        recent_lines = memory.recent_spoken_lines(limit=30) if memory else []
        available = [
            line for line in templates
            if not self._is_similar_response(line.format(count=unread_count), recent_lines)
        ]
        template = self._choose_response(
            "notification_count",
            available or templates,
        )
        return {
            "sprite": "sassy",
            "mood": "annoyed",
            "message": template.format(count=unread_count),
        }

    def _fallback_scores(self, text: str) -> dict[str, float]:
        query = set(_tokenize(text))
        scores: dict[str, float] = {}
        for intent, examples in self._examples_by_intent.items():
            best = 0.0
            for example in examples:
                union = query | example
                if union:
                    best = max(best, len(query & example) / len(union))
            scores[intent] = best
        return scores

    def predict_intent(self, text: str, telemetry: dict[str, Any] | None = None) -> tuple[str, float]:
        if not text.strip():
            raise ValueError("Input text cannot be empty.")
        scores = self._fallback_scores(text)
        intent, confidence = max(scores.items(), key=lambda item: item[1])
        return (intent, confidence) if confidence >= FALLBACK_CONFIDENCE_THRESHOLD else ("unknown", confidence)

    def respond(
        self,
        text: str,
        telemetry: dict[str, Any] | None = None,
        pet_stats: dict[str, Any] | None = None,
        memory: PetMemory | None = None,
    ) -> dict[str, Any]:
        """Respond immediately without loading a neural-network runtime."""
        if not text.strip():
            raise ValueError("Input text cannot be empty.")
        if self._is_system_vitals_query(text):
            emotion = "sarcastic" if (
                float((telemetry or {}).get("cpu_percent", 0.0)) > 80.0
                or float((telemetry or {}).get("ram_percent", 0.0)) > 85.0
            ) else "curious"
            emotional = self._emotion_response(emotion)
            return {
                "intent": "ask_status",
                "mood": self._mood(pet_stats, telemetry),
                "emotion": emotion,
                "sprite": emotional["sprite"],
                "response": self._system_vitals_response(telemetry),
                "confidence": 1.0,
                "backend": self.backend,
            }

        from core.sensors import SystemMonitor

        activity = SystemMonitor._detect_activity(text.casefold())
        if activity not in {"other", "idle_desktop"}:
            reaction = self.activity_reaction(
                activity,
                late_night=bool((telemetry or {}).get("late_night", False)),
            )
            if reaction is not None:
                return {
                    "intent": "activity_comment",
                    "mood": reaction["mood"],
                    "emotion": reaction["mood"],
                    "sprite": reaction["sprite"],
                    "response": reaction["message"],
                    "confidence": 1.0,
                    "backend": self.backend,
                }

        intent, confidence = self.predict_intent(text, telemetry)
        if intent == "unknown":
            return {
                "intent": intent,
                "mood": self._mood(pet_stats, telemetry),
                "emotion": "curious",
                "sprite": "idle",
                "response": UNKNOWN_RESPONSE,
                "confidence": confidence,
                "backend": self.backend,
            }

        mood = self._mood(pet_stats, telemetry)
        emotion = self._INTENT_EMOTIONS.get(intent, "curious")
        emotional = self._emotion_response(emotion)
        recent_openers = (
            memory.recent_response_phrases("opener", limit=10)
            if memory is not None
            else []
        )
        recent_closers = (
            memory.recent_response_phrases("closer", limit=10)
            if memory is not None
            else []
        )
        available_openers = [
            phrase for phrase in MOOD_OPENERS[mood] if phrase not in recent_openers
        ] or list(MOOD_OPENERS[mood])
        available_closers = [
            phrase for phrase in MOOD_CLOSERS[mood] if phrase not in recent_closers
        ] or list(MOOD_CLOSERS[mood])
        opener = self._random.choice(available_openers)
        closer = self._random.choice(available_closers)
        response = f"{opener} {emotional['line'].rstrip(' .!?')}! {closer}"
        return {
            "intent": intent,
            "mood": mood,
            "emotion": emotion,
            "sprite": MOOD_SPRITES[mood] if mood in MOOD_SPRITES else emotional["sprite"],
            "response": response,
            "opener": opener,
            "closer": closer,
            "confidence": confidence,
            "backend": self.backend,
        }

    def proactive_alerts(
        self,
        telemetry: dict[str, Any] | None = None,
        pet_stats: dict[str, Any] | None = None,
    ) -> list[str]:
        sensors = telemetry or {}
        stats = pet_stats or {}
        active: set[str] = set()
        battery = sensors.get("battery_percent")
        if battery is not None and float(battery) <= 15.0:
            active.add("low_battery")
        if sensors.get("late_night", False):
            active.add("late_night")
        if float(stats.get("hunger", 0.0)) >= 75.0:
            active.add("pet_hungry")
        if float(stats.get("energy", 100.0)) <= 20.0:
            active.add("pet_tired")
        return [
            alert["message"]
            for alert in self.dataset["proactive_alerts"]
            if alert["condition"] in active
        ]
