"""Train a small local-only intent model from the bundled pet dataset."""

from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path
from typing import Any

import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence
from torch.utils.data import DataLoader, Dataset


ROOT = Path(__file__).resolve().parent
DATASET_PATH = ROOT / "dataset.json"
MODEL_PATH = ROOT / "model.pt"
TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
SPECIAL_TOKENS = ("<pad>", "<unk>")
TELEMETRY_SIZE = 7


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def read_dataset() -> dict[str, Any]:
    with DATASET_PATH.open("r", encoding="utf-8") as file:
        dataset = json.load(file)
    if not isinstance(dataset.get("intents"), dict) or len(dataset["intents"]) != 8:
        raise ValueError("dataset.json must define exactly eight intents.")
    return dataset


def expand_examples(dataset: dict[str, Any]) -> list[tuple[str, str]]:
    augmentation = dataset["pattern_augmentation"]
    prefixes = augmentation["prefixes"]
    suffixes = augmentation["suffixes"]
    expanded: list[tuple[str, str]] = []
    for intent, content in dataset["intents"].items():
        examples = content["examples"]
        if not examples:
            raise ValueError(f"Intent {intent!r} has no training examples.")
        for example in examples:
            expanded.append((example, intent))
            for prefix in prefixes:
                expanded.append((f"{prefix}{example}", intent))
            for suffix in suffixes:
                expanded.append((f"{example}{suffix}", intent))
            for prefix in prefixes:
                for suffix in suffixes:
                    expanded.append((f"{prefix}{example}{suffix}", intent))
    if len(expanded) < 300:
        raise ValueError(f"Only {len(expanded)} training patterns were created; at least 300 are required.")
    return expanded


def create_vocabulary(examples: list[tuple[str, str]]) -> dict[str, int]:
    tokens = sorted({token for text, _ in examples for token in tokenize(text)})
    return {token: index for index, token in enumerate((*SPECIAL_TOKENS, *tokens))}


def encode(text: str, vocabulary: dict[str, int]) -> list[int]:
    unknown_index = vocabulary["<unk>"]
    return [vocabulary.get(token, unknown_index) for token in tokenize(text)] or [unknown_index]


class IntentDataset(Dataset[tuple[list[int], int]]):
    def __init__(
        self,
        examples: list[tuple[str, str]],
        vocabulary: dict[str, int],
        labels: dict[str, int],
    ) -> None:
        self.samples = [(encode(text, vocabulary), labels[intent]) for text, intent in examples]

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[list[int], int]:
        return self.samples[index]


def collate_batch(
    batch: list[tuple[list[int], int]],
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    lengths = torch.tensor([len(tokens) for tokens, _ in batch], dtype=torch.long)
    max_length = int(lengths.max().item())
    token_ids = torch.zeros((len(batch), max_length), dtype=torch.long)
    for row_index, (tokens, _) in enumerate(batch):
        token_ids[row_index, : len(tokens)] = torch.tensor(tokens, dtype=torch.long)
    labels = torch.tensor([label for _, label in batch], dtype=torch.long)

    telemetry_rows = []
    for _ in batch:
        hour = random.randrange(24)
        telemetry_rows.append(
            [
                random.random(),
                random.random(),
                random.random(),
                float(hour >= 23 or hour < 5),
                float(random.random() < 0.45),
                (hour / 23.0),
                (math.sin(2.0 * math.pi * hour / 24.0) + 1.0) / 2.0,
            ]
        )
    telemetry = torch.tensor(telemetry_rows, dtype=torch.float32)
    return token_ids, lengths, telemetry, labels


class IntentClassifier(nn.Module):
    def __init__(self, vocabulary_size: int, intent_count: int, embedding_size: int = 48) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocabulary_size, embedding_size, padding_idx=0)
        self.lstm = nn.LSTM(
            input_size=embedding_size,
            hidden_size=48,
            batch_first=True,
            bidirectional=True,
        )
        self.telemetry_layer = nn.Sequential(
            nn.Linear(TELEMETRY_SIZE, 16),
            nn.ReLU(),
        )
        self.classifier = nn.Sequential(
            nn.Linear(96 + 16, 64),
            nn.ReLU(),
            nn.Dropout(0.15),
            nn.Linear(64, intent_count),
        )

    def forward(
        self,
        token_ids: torch.Tensor,
        lengths: torch.Tensor,
        telemetry: torch.Tensor,
    ) -> torch.Tensor:
        embedded = self.embedding(token_ids)
        packed = pack_padded_sequence(
            embedded,
            lengths.cpu(),
            batch_first=True,
            enforce_sorted=False,
        )
        _, (hidden, _) = self.lstm(packed)
        text_features = torch.cat((hidden[-2], hidden[-1]), dim=1)
        telemetry_features = self.telemetry_layer(telemetry)
        return self.classifier(torch.cat((text_features, telemetry_features), dim=1))


def train(epochs: int = 40, batch_size: int = 64) -> Path:
    if epochs < 1 or batch_size < 1:
        raise ValueError("Epoch count and batch size must be positive.")
    random.seed(41)
    torch.manual_seed(41)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(41)

    dataset = read_dataset()
    examples = expand_examples(dataset)
    intents = list(dataset["intents"])
    labels = {intent: index for index, intent in enumerate(intents)}
    vocabulary = create_vocabulary(examples)
    training_data = IntentDataset(examples, vocabulary, labels)
    loader = DataLoader(
        training_data,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=collate_batch,
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = IntentClassifier(len(vocabulary), len(intents)).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.003, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss()
    model.train()
    for epoch in range(epochs):
        epoch_loss = 0.0
        for token_ids, lengths, telemetry, targets in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(
                token_ids.to(device),
                lengths,
                telemetry.to(device),
            )
            loss = criterion(logits, targets.to(device))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_loss += float(loss.item())
        if epoch == 0 or (epoch + 1) % 10 == 0 or epoch + 1 == epochs:
            print(f"epoch {epoch + 1:02d}/{epochs} loss={epoch_loss / len(loader):.4f}")

    model.to("cpu")
    checkpoint = {
        "state_dict": model.state_dict(),
        "vocabulary": vocabulary,
        "intents": intents,
        "embedding_size": 48,
        "telemetry_size": TELEMETRY_SIZE,
        "training_patterns": len(examples),
        "device": str(device),
    }
    torch.save(checkpoint, MODEL_PATH)
    print(f"Trained on {len(examples)} patterns using {device}; saved {MODEL_PATH}")
    return MODEL_PATH


if __name__ == "__main__":
    train()
