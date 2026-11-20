"""Fit the small trigram response scorer from the bundled offline corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT.parent))

from brain.dialogue import ACTIVITIES, MOODS, compose_candidates
from brain.markov import collect_dataset_texts, write_ngram_model

DATASET_PATH = ROOT / "dataset.json"
CORPUS_PATH = ROOT / "tsundere_corpus.jsonl"
MODEL_PATH = ROOT / "ngram_model.json"


def _read_dataset(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read training dataset {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Training dataset at {path} must contain a JSON object.")
    return value


def _read_generated_corpus(path: Path) -> list[str]:
    if not path.is_file():
        return []
    documents: list[str] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid generated corpus record on line {line_number}: {error}"
                ) from error
            text = record.get("response") if isinstance(record, dict) else None
            if isinstance(text, str) and text.strip():
                documents.append(text.strip())
    return documents


def train(
    dataset_path: str | Path = DATASET_PATH,
    corpus_path: str | Path = CORPUS_PATH,
    model_path: str | Path = MODEL_PATH,
) -> Path:
    """Train and save the local trigram scorer without Torch, CUDA, or downloads."""
    dataset_file = Path(dataset_path)
    dataset = _read_dataset(dataset_file)
    documents = collect_dataset_texts(dataset)
    documents.extend(_read_generated_corpus(Path(corpus_path)))
    for activity in ACTIVITIES:
        for mood in MOODS:
            documents.extend(
                compose_candidates(
                    activity,
                    {
                        "title": "a sample local activity",
                        "artist": "a sample artist",
                        "app": "the foreground app",
                        "ram_gb": "8.0",
                        "unread_count": 8,
                    },
                    mood,
                )
            )
    output = Path(model_path)
    write_ngram_model(documents, output)
    print(
        f"Trained trigram scorer from {len(documents):,} local dialogue samples; "
        f"saved {output}"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--model", type=Path, default=MODEL_PATH)
    args = parser.parse_args()
    train(args.dataset, args.corpus, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
