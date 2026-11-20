"""Small local trigram language model for ranking response templates."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

WORD_PATTERN = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)?", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [word.casefold() for word in WORD_PATTERN.findall(text)]


def build_ngram_model(documents: Iterable[str]) -> dict[str, Any]:
    unigrams: Counter[str] = Counter()
    bigrams: Counter[str] = Counter()
    trigrams: Counter[str] = Counter()
    for document in documents:
        words = ["<s>", "<s>", *tokenize(document), "</s>"]
        unigrams.update(words[2:])
        bigrams.update(f"{left} {right}" for left, right in zip(words, words[1:]))
        trigrams.update(
            f"{first} {second} {third}"
            for first, second, third in zip(words, words[1:], words[2:])
        )
    return {
        "version": 1,
        "vocabulary_size": max(1, len(unigrams)),
        "unigrams": dict(unigrams),
        "bigrams": dict(bigrams),
        "trigrams": dict(trigrams),
    }


def collect_dataset_texts(value: Any) -> list[str]:
    documents: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if tokenize(item):
                documents.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return documents


def load_ngram_model(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as source:
            model = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load local language model at {path}: {error}") from error
    if not isinstance(model, dict) or model.get("version") != 1:
        raise ValueError(f"Unsupported trigram language model format in {path}.")
    for key in ("unigrams", "bigrams", "trigrams"):
        if not isinstance(model.get(key), dict):
            raise ValueError(f"Trigram language model at {path} is missing {key!r}.")
    return model


class TrigramScorer:
    def __init__(self, model: dict[str, Any]) -> None:
        self._unigrams: dict[str, int] = model["unigrams"]
        self._bigrams: dict[str, int] = model["bigrams"]
        self._trigrams: dict[str, int] = model["trigrams"]
        self._vocabulary_size = max(1, int(model.get("vocabulary_size", len(self._unigrams))))
        self._unigram_total = sum(self._unigrams.values())

    def score(self, text: str) -> float:
        words = ["<s>", "<s>", *tokenize(text), "</s>"]
        if len(words) <= 3:
            return float("-inf")
        log_probability = 0.0
        token_count = 0
        for first, second, third in zip(words, words[1:], words[2:]):
            context = f"{first} {second}"
            context_count = self._bigrams.get(context, 0)
            trigram_count = self._trigrams.get(f"{context} {third}", 0)
            if context_count:
                probability = (trigram_count + 0.2) / (
                    context_count + 0.2 * self._vocabulary_size
                )
            else:
                probability = (
                    self._unigrams.get(third, 0) + 0.2
                ) / (self._unigram_total + 0.2 * self._vocabulary_size)
            log_probability += math.log(probability)
            token_count += 1
        return log_probability / max(1, token_count)


def write_ngram_model(documents: Iterable[str], path: Path) -> dict[str, Any]:
    model = build_ngram_model(documents)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(model, output, ensure_ascii=False, separators=(",", ":"))
        temporary_path.replace(path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
    return model
