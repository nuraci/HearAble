from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BackendConfigError(ValueError):
    """Raised when an audio source or subtitle sink config is invalid."""


@dataclass(frozen=True)
class BackendConfig:
    path: str
    type: str
    options: dict[str, Any]


def load_backend_config(path: str, *, kind: str, root: Path) -> BackendConfig:
    if not path:
        raise BackendConfigError(f"{kind} config path is empty")
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        values = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackendConfigError(f"{kind} config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BackendConfigError(f"{kind} config malformed JSON in {config_path}: {exc.msg}") from exc
    if not isinstance(values, dict):
        raise BackendConfigError(f"{kind} config must be a JSON object: {config_path}")
    type_name = values.get("type")
    if not isinstance(type_name, str) or not type_name.strip():
        raise BackendConfigError(f"{kind} config missing string 'type': {config_path}")
    options = values.get("options", {})
    if not isinstance(options, dict):
        raise BackendConfigError(f"{kind} config 'options' must be an object: {config_path}")
    return BackendConfig(path=str(config_path), type=type_name.strip(), options=dict(options))


def resolve_path(value: str, *, root: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else root / path)
