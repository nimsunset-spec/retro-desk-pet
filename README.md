# Retro 90s Desktop Pet

A tiny, fully offline desktop companion built with Python and PyQt6. The pet floats above your desktop, reacts to chat and care actions, remembers its stats in SQLite, and offers local system-awareness without calling an online service.

## Features

- Frameless, transparent, always-on-top sprite window with drag-to-move behavior.
- Gentle mood-specific sprite animations and click-to-chat access.
- Original 64x64 pixel-art states generated locally with Pillow.
- Right-click and system-tray menus for Feed, Pet, Stats, and Quit.
- Hidden-by-default typewriter speech bubble with a built-in chat field and timed fade-out.
- SQLite persistence for name, hunger, energy, happiness, friendship XP, and chat history.
- Local CPU, RAM, battery, clock, active-window, and coding-session telemetry.
- Local app-category and active-window-title-change counts; notification history stores categories and counts, not window titles or notification contents.
- Offline intent brain: 320 hand-written examples across eight intents, expanded into 2,880 local training patterns for a custom PyTorch word-embedding/BiLSTM model with telemetry fusion, plus a scikit-learn TF-IDF fallback and original response templates.
- Intent-aware emotional dialogue with supportive, playful, sleepy, and sarcastic tones plus anti-repeat response history.
- Periodic, rate-limited proactive alerts for system and pet status.

## Privacy and offline operation

The application makes no API calls and downloads no language model. Training uses only `brain/dataset.json` and locally simulated telemetry. All conversations and pet stats remain in the local SQLite database at `data/pet.sqlite3`.

The PyTorch network is initialized and trained from scratch. To keep normal startup lightweight, the engine loads it only when `brain/model.pt` exists; without that checkpoint it uses the local TF-IDF fallback.

## Setup

Use Python 3.10 or newer. From the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python assets\generate_sprites.py
python brain\train.py
python main.py
```

On Linux, activate with `source .venv/bin/activate` and use the same Python commands. A desktop session is required for the transparent overlay. Some Linux window managers do not expose active-window titles; the monitor reports that telemetry as unavailable when the platform does not provide it.

## Architecture

```text
main.py                 Application wiring and 60-second alert timer
ui/pet_window.py        Animated transparent pet, dragging, context menu, tray icon
ui/chat_bubble.py       Auto-hiding pixel speech panel and chat input
core/memory.py          SQLite state, decay, care actions, and chat history
core/sensors.py         Background local telemetry sampling
brain/dataset.json      Intent examples, response pools, and alert templates
brain/train.py          Scratch-trained BiLSTM with telemetry fusion
brain/engine.py         Inference, emotional dialogue, and response generation
assets/generate_sprites.py  Local transparent pixel-art sprite generation
```

The generated sprite PNGs and trained `brain/model.pt` are local runtime artifacts. They can be recreated at any time using the commands above.

## Notes

- PyGetWindow may not be able to read active-window titles in every Linux desktop session; CPU, RAM, battery, and clock telemetry continue independently.
- On Linux, X11 active-window detection can use `xdotool`; Wayland may restrict window-title access. Browser tracking uses visible active-window titles only and does not read page contents or search text.
- Training selects CUDA when PyTorch detects a compatible GPU and otherwise uses CPU. Inference is always performed on CPU.
- A system tray is shown when the desktop environment provides one.
