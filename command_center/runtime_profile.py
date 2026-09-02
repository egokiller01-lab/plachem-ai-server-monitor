from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    provider: str
    model: str
    base_url: str | None
    options: dict[str, Any]


class RuntimeProfileResolver:
    """Resolve the selected agent's current Hermes profile configuration."""

    def __init__(self, hermes_home: str | Path):
        self.hermes_home = Path(hermes_home)

    def resolve(self, profile_name: str) -> RuntimeProfile:
        if not isinstance(profile_name, str) or not profile_name:
            raise ValueError("INVALID_RUNTIME_PROFILE")
        path = self.hermes_home / "profiles" / profile_name / "config.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"RUNTIME_PROFILE_UNAVAILABLE:{profile_name}") from exc
        model = raw.get("model") if isinstance(raw, dict) else None
        if not isinstance(model, dict):
            raise ValueError(f"INVALID_RUNTIME_PROFILE:{profile_name}")
        provider = model.get("provider")
        selected_model = model.get("default")
        base_url = model.get("base_url")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(f"INVALID_RUNTIME_PROFILE:{profile_name}")
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise ValueError(f"INVALID_RUNTIME_PROFILE:{profile_name}")
        if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
            raise ValueError(f"INVALID_RUNTIME_PROFILE:{profile_name}")
        options = {key: value for key, value in model.items() if key not in {"provider", "default", "base_url"}}
        return RuntimeProfile(profile_name, provider, selected_model, base_url, options)
