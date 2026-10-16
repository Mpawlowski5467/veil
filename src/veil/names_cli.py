"""Explicit controls for optional local name recognition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .entities_cli import edit_config
from .gateway.config import Settings, SettingsError, load_settings, name_detector


def configure_names(data_dir: Path, model: Path | None) -> bool:
    """Validate a local model before persisting a change; None disables it."""
    value = str(model.expanduser().absolute()) if model is not None else None
    if value is not None:
        name_detector(Settings(local_names_model=value))

    def change(raw: dict[str, Any]) -> bool:
        if raw.get("local_names_model") == value:
            return False
        raw["local_names_model"] = value
        return True

    return edit_config(data_dir, change)


def run_names(
    operation: str, *, data_dir: Path, model: Path | None, json_output: bool = False
) -> int:
    """Enable, disable, or report recognition without exposing model paths."""
    if operation == "status":
        settings = load_settings(data_dir / "config.json")
        enabled = settings.local_names_model is not None
        result = {"configured": enabled, "ready": False, "scope": "local names only"}
        if enabled:
            name_detector(settings)
            result["ready"] = True
        if json_output:
            print(json.dumps(result))
        else:
            print("Local name recognition: " + ("ready" if enabled else "disabled"))
        return 0
    if operation == "enable" and model is None:
        raise SettingsError("select a local model with --model PATH")
    configure_names(data_dir, model if operation == "enable" else None)
    print("Local name recognition " + ("enabled." if model else "disabled."))
    print("Restart running gateways or relaunch Veil clients to apply the change.")
    return 0
