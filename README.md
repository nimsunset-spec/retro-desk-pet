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
- App mentions and detected app/window changes use their matching activity-specific response pools; uncertain chat input gets a clarification instead of an unrelated intent guess.
- Intent-aware emotional dialogue with supportive, playful, sleepy, and sarcastic tones plus anti-repeat response history.
- Periodic, rate-limited proactive alerts for system and pet status.

## Privacy and offline operation

The application makes no API calls and downloads no language model. Training uses only `brain/dataset.json` and locally simulated telemetry. Conversations and pet stats remain in a local SQLite database on the user's device.

The PyTorch network is initialized and trained from scratch. To keep normal startup lightweight, the engine loads it only when `brain/model.pt` exists; without that checkpoint it uses the local TF-IDF fallback.

## Setup

### Windows: standalone app (no Python required)

Download and run the latest Windows bundle from [GitHub Releases](https://github.com/nimsunset-spec/retro-desk-pet/releases/latest). Extract the ZIP, then double-click `RetroDeskPet.exe`.

To install it from PowerShell and launch it immediately:

```powershell
$install = Join-Path $env:LOCALAPPDATA "Programs\RetroDeskPet"
$zip = Join-Path $env:TEMP "RetroDeskPet-windows-x64.zip"
Invoke-WebRequest "https://github.com/nimsunset-spec/retro-desk-pet/releases/latest/download/RetroDeskPet-windows-x64.zip" -OutFile $zip
Expand-Archive -LiteralPath $zip -DestinationPath $install -Force
Start-Process (Join-Path $install "RetroDeskPet.exe")
```

For later launches, run:

```powershell
& "$env:LOCALAPPDATA\Programs\RetroDeskPet\RetroDeskPet.exe"
```

This bundle includes its Python runtime and lightweight offline dialogue fallback. It does not include the optional PyTorch training/inference stack. The first public Windows bundle is produced by the tagged GitHub Actions release workflow. The executable is unsigned, so Windows may show a publisher warning.

### Install from GitHub with pipx

This alternative installs the Python app into an isolated environment. It needs Python and Git, but does not install the optional PyTorch/scikit-learn training stack:

```powershell
pipx install https://github.com/nimsunset-spec/retro-desk-pet/archive/refs/heads/main.zip
retro-pet
```

### Run from a development checkout

Use Python 3.10 or newer from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

On Linux, activate with `source .venv/bin/activate` and use the same Python commands. `requirements.txt` includes the optional training dependencies. To regenerate sprites or retrain the local model, run `python assets\generate_sprites.py` or `python brain\train.py` on Windows, and use `/` separators on Linux.

A desktop session is required for the transparent overlay. Some Linux window managers do not expose active-window titles; the monitor reports that telemetry as unavailable when the platform does not provide it.

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

The installed app stores its SQLite database in the current user's application-data folder (`%LOCALAPPDATA%\RetroDeskPet` on Windows, or `$XDG_DATA_HOME/retro-desk-pet` on Linux; the default Linux base is `~/.local/share`). Existing `data/pet.sqlite3` files remain in use when running directly from a source checkout.

## Notes

- PyGetWindow may not be able to read active-window titles in every Linux desktop session; CPU, RAM, battery, and clock telemetry continue independently.
- On Linux, X11 active-window detection can use `xdotool`; Wayland may restrict window-title access. Browser tracking uses visible active-window titles only and does not read page contents or search text.
- Training selects CUDA when PyTorch detects a compatible GPU and otherwise uses CPU. Inference is always performed on CPU.
- A system tray is shown when the desktop environment provides one.
