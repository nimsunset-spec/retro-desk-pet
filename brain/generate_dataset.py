"""Generate varied, fully formed local tsundere dialogue examples."""

from __future__ import annotations

import argparse
import json
import sys
from itertools import count
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT.parent))

from brain.dialogue import ACTIVITIES, MOODS, compose_candidates

DEFAULT_OUTPUT = ROOT / "tsundere_corpus.jsonl"

SCENARIOS: dict[str, tuple[dict[str, Any], ...]] = {
    "youtube_music": tuple(
        {"title": title, "artist": artist, "app": "YouTube"}
        for title, artist in (
            ("Instant Crush", "Daft Punk"),
            ("Take On Me", "a-ha"),
            ("Bohemian Rhapsody", "Queen"),
            ("Blinding Lights", "The Weeknd"),
            ("a synthwave mix", "various artists"),
        )
    ),
    "youtube_video": tuple(
        {"title": title, "app": "YouTube"}
        for title in (
            "a keyboard repair guide",
            "a Blender tutorial",
            "a speedrun breakdown",
            "a cooking lesson",
            "a science documentary",
        )
    ),
    "spotify": tuple(
        {"title": title, "artist": artist, "app": "Spotify"}
        for title, artist in (
            ("Instant Crush", "Daft Punk"),
            ("No Surprises", "Radiohead"),
            ("Sweden", "C418"),
            ("From the Start", "Laufey"),
            ("a lo-fi study mix", "various artists"),
        )
    ),
    "pinterest": tuple(
        {"title": title, "app": "Pinterest"}
        for title in (
            "pixel-art character ideas",
            "cozy desk layouts",
            "retro computer builds",
            "anime sketch references",
            "weekend craft projects",
        )
    ),
    "social_media": tuple(
        {"title": title, "app": app}
        for app, title in (
            ("Reddit", "a programming discussion"),
            ("X", "your timeline"),
            ("Instagram", "an art feed"),
            ("Discord", "a study server"),
            ("TikTok", "a short video feed"),
        )
    ),
    "coding": tuple(
        {"title": title, "app": "Visual Studio Code"}
        for title in (
            "sensors.py in the pet project",
            "a failing unit test",
            "a PyQt widget",
            "a SQLite migration",
            "a new feature branch",
        )
    ),
    "gaming": tuple(
        {"title": title, "app": title}
        for title in ("Minecraft", "Stardew Valley", "Hades", "Celeste", "Terraria")
    ),
    "idle_desktop": tuple(
        {"title": "the desktop", "app": "Windows"}
        for _ in range(5)
    ),
    "high_ram": tuple(
        {"title": "memory use", "app": app, "ram_gb": ram}
        for app, ram in (
            ("Chrome", "8.0"),
            ("Visual Studio Code", "9.2"),
            ("a background browser", "7.4"),
            ("several open apps", "10.1"),
            ("a video editor", "11.3"),
        )
    ),
    "stacked_notifications": tuple(
        {"title": "the Action Center", "app": app, "unread_count": unread}
        for app, unread in (
            ("Discord", 8),
            ("Mail", 12),
            ("team chat", 17),
            ("social apps", 9),
            ("your apps", 21),
        )
    ),
    "user_chat": tuple(
        {"title": text, "app": "you"}
        for text in (
            "your latest message",
            "that question",
            "your friendly hello",
            "your tiny joke",
            "your helpful reminder",
        )
    ),
}


def iter_examples(example_count: int = 20_000) -> Iterator[dict[str, str]]:
    """Yield context/response pairs spanning every activity, mood, and clause set."""
    minimum = len(ACTIVITIES) * len(MOODS)
    if example_count < minimum:
        raise ValueError(f"example_count must be at least {minimum}.")
    maximum = minimum * 75 * min(len(entities) for entities in SCENARIOS.values())
    if example_count > maximum:
        raise ValueError(f"At most {maximum:,} unique response patterns are defined.")

    cycle_length = minimum * 75
    for index in count():
        if index >= example_count:
            return
        activity = ACTIVITIES[index % len(ACTIVITIES)]
        mood_index = (index // len(ACTIVITIES)) % len(MOODS)
        mood = MOODS[mood_index]
        pattern_index = (index // minimum) % 75
        entity_index = (index // cycle_length) % len(SCENARIOS[activity])
        values = SCENARIOS[activity][entity_index]
        response = compose_candidates(activity, values, mood)[pattern_index]
        context = (
            f"<ctx>activity={activity}|title={values.get('title', '')}|"
            f"artist={values.get('artist', '')}|app={values.get('app', '')}|"
            f"mood={mood}|ram_gb={values.get('ram_gb', '')}|"
            f"unread_count={values.get('unread_count', 0)}</ctx>"
        )
        yield {
            "context": context,
            "response": response,
            "text": f"{context}<pet>{response}</pet>",
        }


def write_dataset(
    output_path: str | Path = DEFAULT_OUTPUT,
    example_count: int = 20_000,
) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        for example in iter_examples(example_count):
            output.write(json.dumps(example, ensure_ascii=False) + "\n")
            written += 1
    if written != example_count:
        raise RuntimeError(
            f"Only generated {written} of {example_count} requested examples."
        )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=20_000)
    args = parser.parse_args()
    generated = write_dataset(args.output, args.count)
    print(f"Wrote {generated:,} original local examples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
