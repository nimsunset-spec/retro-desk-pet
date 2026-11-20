"""Offline intent prediction and retro-styled pet response generation."""

from __future__ import annotations

import json
import math
import random
import re
from collections import deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
INTENT_THRESHOLD = 0.38
FALLBACK_CONFIDENCE_THRESHOLD = 0.30
UNKNOWN_RESPONSE = (
    "I am not sure what you mean yet. Try asking about my stats or your PC, "
    "tell me how you feel, or mention an app you have open."
)


def _tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


class BrainEngine:
    """Combine the locally trained model, TF-IDF fallback, and templates."""

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
        with (ROOT / "dataset.json").open("r", encoding="utf-8") as file:
            self.dataset: dict[str, Any] = json.load(file)
        self.intents = list(self.dataset["intents"])
        self._random = random.Random(seed)
        self._response_history: dict[str, deque[str]] = {}
        self._model: Any = None
        self._torch: Any = None
        self._vocabulary: dict[str, int] = {}
        self._backend_error: str | None = None
        self._vectorizer: Any = None
        self._training_vectors: Any = None
        self._training_labels: list[str] = []
        self._initialize_fallback()
        self._initialize_model()

    @property
    def backend(self) -> str:
        if self._model is not None:
            return "pytorch"
        if self._vectorizer is not None:
            return "tfidf"
        return "token-overlap"

    @property
    def backend_error(self) -> str | None:
        return self._backend_error

    def _choose_response(self, category: str, responses: list[str]) -> str:
        if not responses:
            raise ValueError(f"No response templates configured for {category!r}.")
        history = self._response_history.setdefault(category, deque(maxlen=5))
        available = [response for response in responses if response not in history]
        selected = self._random.choice(available or responses)
        history.append(selected)
        return selected

    def _examples(self) -> list[tuple[str, str]]:
        augmentation = self.dataset["pattern_augmentation"]
        prefixes = augmentation["prefixes"]
        suffixes = augmentation["suffixes"]
        examples: list[tuple[str, str]] = []
        for intent, content in self.dataset["intents"].items():
            for text in content["examples"]:
                examples.append((text, intent))
                examples.extend((f"{prefix}{text}", intent) for prefix in prefixes)
                examples.extend((f"{text}{suffix}", intent) for suffix in suffixes)
                examples.extend(
                    (f"{prefix}{text}{suffix}", intent)
                    for prefix in prefixes
                    for suffix in suffixes
                )
        return examples

    def _initialize_fallback(self) -> None:
        examples = self._examples()
        self._training_labels = [intent for _, intent in examples]
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            self._vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
            self._training_vectors = self._vectorizer.fit_transform([text for text, _ in examples])
        except ImportError as error:
            self._backend_error = f"scikit-learn unavailable; using token overlap: {error}"

    def _initialize_model(self) -> None:
        checkpoint_path = ROOT / "model.pt"
        if not checkpoint_path.is_file():
            self._backend_error = "No local model checkpoint found; using the offline fallback."
            return
        try:
            import torch
            from brain.train import IntentClassifier

            torch.set_num_threads(1)
            checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
            model = IntentClassifier(
                len(checkpoint["vocabulary"]),
                len(checkpoint["intents"]),
                embedding_size=int(checkpoint["embedding_size"]),
            )
            model.load_state_dict(checkpoint["state_dict"])
            model.to("cpu")
            model.eval()
        except (ImportError, OSError, RuntimeError, KeyError, ValueError) as error:
            self._backend_error = f"Local model could not be loaded; using fallback: {error}"
            return
        self._torch = torch
        self._model = model
        self._vocabulary = checkpoint["vocabulary"]
        self.intents = checkpoint["intents"]
        self._backend_error = None

    @staticmethod
    def _telemetry_features(telemetry: dict[str, Any] | None) -> list[float]:
        values = telemetry or {}
        cpu = max(0.0, min(100.0, float(values.get("cpu_percent", 0.0)))) / 100.0
        ram = max(0.0, min(100.0, float(values.get("ram_percent", 0.0)))) / 100.0
        battery_value = values.get("battery_percent")
        battery = 0.5 if battery_value is None else max(
            0.0, min(100.0, float(battery_value))
        ) / 100.0
        hour = int(values.get("hour", 12)) % 24
        angle = 2.0 * math.pi * hour / 24.0
        return [
            cpu,
            ram,
            battery,
            float(bool(values.get("late_night", False))),
            float(bool(values.get("coding_detected", False))),
            hour / 23.0,
            (math.sin(angle) + 1.0) / 2.0,
        ]

    def _model_prediction(
        self,
        text: str,
        telemetry: dict[str, Any] | None,
    ) -> tuple[str, float] | None:
        if self._model is None:
            return None
        torch = self._torch
        token_ids = [
            self._vocabulary.get(token, self._vocabulary["<unk>"])
            for token in _tokenize(text)
        ]
        if not token_ids:
            token_ids = [self._vocabulary["<unk>"]]
        with torch.inference_mode():
            logits = self._model(
                torch.tensor([token_ids], dtype=torch.long),
                torch.tensor([len(token_ids)], dtype=torch.long),
                torch.tensor([self._telemetry_features(telemetry)], dtype=torch.float32),
            )
            probabilities = torch.softmax(logits[0], dim=0)
            score, index = torch.max(probabilities, dim=0)
        return self.intents[int(index.item())], float(score.item())

    def _fallback_scores(self, text: str) -> dict[str, float]:
        scores = {intent: 0.0 for intent in self.intents}
        if self._vectorizer is not None and self._training_vectors is not None:
            vector = self._vectorizer.transform([text])
            similarities = (self._training_vectors @ vector.T).toarray().ravel()
            for label, similarity in zip(self._training_labels, similarities):
                scores[label] = max(scores[label], float(similarity))
        else:
            query_tokens = set(_tokenize(text))
            if not query_tokens:
                return scores
            for intent, content in self.dataset["intents"].items():
                best = 0.0
                for example in content["examples"]:
                    example_tokens = set(_tokenize(example))
                    union = query_tokens | example_tokens
                    if union:
                        best = max(best, len(query_tokens & example_tokens) / len(union))
                scores[intent] = best
        return scores

    def predict_intent(
        self,
        text: str,
        telemetry: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        """Return the most likely intent and a confidence score."""
        if not text.strip():
            raise ValueError("Input text cannot be empty.")
        neural = self._model_prediction(text, telemetry)
        fallback_scores = self._fallback_scores(text)
        fallback_intent, fallback_score = max(
            fallback_scores.items(),
            key=lambda item: item[1],
        )
        if fallback_score < FALLBACK_CONFIDENCE_THRESHOLD:
            return "unknown", fallback_score
        if neural is None:
            return fallback_intent, fallback_score
        neural_intent, neural_score = neural
        if neural_intent == fallback_intent and neural_score >= INTENT_THRESHOLD:
            return neural_intent, neural_score
        return fallback_intent, fallback_score

    def _mood(self, pet_stats: dict[str, Any] | None, telemetry: dict[str, Any] | None) -> str:
        stats = pet_stats or {}
        sensors = telemetry or {}
        if (
            float(stats.get("happiness", 80.0)) < 30.0
            or float(stats.get("energy", 80.0)) < 25.0
        ):
            return "tired"
        if float(stats.get("happiness", 80.0)) >= 70.0 and not sensors.get("late_night", False):
            return "cheerful"
        return "neutral"

    def respond(
        self,
        text: str,
        telemetry: dict[str, Any] | None = None,
        pet_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict the intent and compose one original offline response."""
        if self._is_system_vitals_query(text):
            sensors = telemetry or {}
            temperature = self._telemetry_number(sensors, "cpu_temperature_c")
            if temperature is None:
                temperature = self._telemetry_number(sensors, "system_temperature_c")
            hardware_is_hot = (
                (self._telemetry_number(sensors, "cpu_percent") or 0.0) > 80.0
                or (self._telemetry_number(sensors, "ram_percent") or 0.0) > 85.0
                or (temperature is not None and temperature > 80.0)
            )
            emotion = "sarcastic" if hardware_is_hot else "curious"
            emotional_response = self._emotion_response(emotion)
            return {
                "intent": "ask_status",
                "mood": self._mood(pet_stats, telemetry),
                "emotion": emotion,
                "sprite": emotional_response["sprite"],
                "response": (
                    f"{self._system_vitals_response(telemetry)} "
                    f"{emotional_response['line']}"
                ),
                "confidence": 1.0,
                "backend": self.backend,
            }
        from core.sensors import SystemMonitor

        activity = SystemMonitor._detect_activity(text.lower())
        if activity not in {"other", "idle_desktop"}:
            reaction = self.activity_reaction(
                activity,
                late_night=bool((telemetry or {}).get("late_night", False)),
            )
            if reaction is not None:
                return {
                    "intent": "activity_comment",
                    "mood": self._mood(pet_stats, telemetry),
                    "emotion": "curious",
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
        if mood == "tired" and intent != "vent_frustration":
            emotion = "sleepy"
        emotional_response = self._emotion_response(emotion)
        return {
            "intent": intent,
            "mood": mood,
            "emotion": emotion,
            "sprite": emotional_response["sprite"],
            "response": emotional_response["line"],
            "confidence": confidence,
            "backend": self.backend,
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
        cpu_temperature = self._telemetry_number(sensors, "cpu_temperature_c")
        system_temperature = self._telemetry_number(sensors, "system_temperature_c")
        temperatures = [
            f"CPU temp at {cpu_temperature:.0f}°C" if cpu_temperature is not None else "",
            (
                f"system temp at {system_temperature:.0f}°C"
                if system_temperature is not None
                and system_temperature != cpu_temperature
                else ""
            ),
        ]
        available_temperatures = [temperature for temperature in temperatures if temperature]
        if available_temperatures:
            hottest = max(value for value in (cpu_temperature, system_temperature) if value is not None)
            temperature_summary = ", ".join(available_temperatures)
            circuits = "getting toasty" if hottest > 80 else "running cool"
            message += f", {temperature_summary}, and my 1997 circuits are {circuits}!"
        else:
            message += ", and temperature sensors are unavailable on this system."

        battery = self._telemetry_number(sensors, "battery_percent")
        if battery is not None:
            charging = sensors.get("is_charging")
            power_state = "charging" if charging else "on battery"
            message += f" Battery at {battery:.0f}% ({power_state})."
        return message

    def activity_reaction(
        self,
        activity: str,
        late_night: bool = False,
    ) -> dict[str, str] | None:
        """Return a varied dataset-authored reaction for an activity."""
        reaction_key = "coding_late_night" if activity == "coding" and late_night else activity
        reaction = self.dataset.get("activity_reactions", {}).get(reaction_key)
        if reaction is None:
            return None
        lines = reaction.get("lines") or [reaction["message"]]
        return {
            "sprite": reaction["sprite"],
            "message": self._choose_response(f"activity:{reaction_key}", lines),
        }

    def hardware_reaction(
        self,
        event: str,
        telemetry: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """Choose a non-repeating line for a hardware or charger event."""
        reaction = self.dataset.get("hardware_reactions", {}).get(event)
        if reaction is None:
            return None
        sensors = telemetry or {}
        value_key = {
            "high_cpu": "cpu_percent",
            "high_ram": "ram_percent",
            "hot_temperature": "cpu_temperature_c",
        }.get(event)
        value = None if value_key is None else self._telemetry_number(sensors, value_key)
        if event == "hot_temperature" and value is None:
            value = self._telemetry_number(sensors, "system_temperature_c")
        templates = reaction["lines"]
        if value is None:
            value = 0.0
        selected = self._choose_response(f"hardware:{event}", templates)
        message = selected.format(value=value)
        if value_key is not None and "{value:" not in selected:
            if event == "hot_temperature":
                message += f" Sensor reading: {value:.1f}°C."
            else:
                metric = "CPU" if event == "high_cpu" else "RAM"
                message += f" {metric} is at {value:.0f}%."
        return {"sprite": reaction["sprite"], "message": message}

    def idle_daydream(self) -> dict[str, str]:
        """Return a non-repeating idle thought for a quiet moment."""
        reaction = self.hardware_reaction("idle_thought")
        if reaction is None:
            raise RuntimeError("The idle-thought response bank is missing.")
        return reaction

    def proactive_alerts(
        self,
        telemetry: dict[str, Any] | None = None,
        pet_stats: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return alerts whose local telemetry or pet-state conditions apply."""
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
