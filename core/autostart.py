"""Manage the current user's Windows startup entry for Retro Desktop Pet."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "RetroDeskPet"


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        executable = os.path.abspath(sys.executable)
        return f'"{executable}"'

    executable = os.path.abspath(sys.executable)
    script = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{executable}" "{script}"'


def is_autostart_enabled() -> bool:
    """Return whether this user's Run key contains a RetroDeskPet entry."""
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        return False
    return True


def enable_autostart() -> bool:
    """Register this executable or development script for the current user."""
    if sys.platform != "win32":
        return False

    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        _RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            _VALUE_NAME,
            0,
            winreg.REG_SZ,
            _startup_command(),
        )
    return True


def disable_autostart() -> bool:
    """Remove this user's startup entry, if present."""
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        return True
    return True


def sync_autostart_path() -> bool:
    """Update an enabled startup entry to the current installation path."""
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            current_value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            current_command = _startup_command()
            if current_value != current_command:
                winreg.SetValueEx(
                    key,
                    _VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    current_command,
                )
    except FileNotFoundError:
        return False
    return True
