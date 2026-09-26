"""Shared, read-only War Room view of OpenClaw and Fast Gateway agents.

OpenClaw owns agent identity/model/workspace configuration.  The Fast Gateway
registry owns execution admission and capabilities.  War Room joins those two
sources; it does not copy or override either source.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LEGACY_DISPLAY = {
    "main": "main",
    "erpcoder": "ERPcoder",
    "erpmanager": "ERPmanager",
    "erpqa": "ERPqa",
}


@dataclass(frozen=True)
class WarRoomAgent:
    agent_id: str
    registered: bool
    gateway_allowed: bool
    enabled: bool
    capabilities: tuple[str, ...]

    @property
    def execution_eligible(self) -> bool:
        return self.registered and self.gateway_allowed and self.enabled


def _openclaw_agent_ids(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    agents = raw.get("agents", {}) if isinstance(raw, dict) else {}
    if not isinstance(agents, dict):
        return []
    # OpenClaw 9.3 registers agents under agents.entries. Keep agents.list
    # only as a compatibility fallback for older isolated fixtures.
    entries = agents.get("entries")
    if isinstance(entries, dict):
        candidates = (value.strip() for value in entries if isinstance(value, str))
    else:
        values = agents.get("list", [])
        candidates = (
            value["id"].strip()
            for value in values
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        ) if isinstance(values, list) else ()
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if _SAFE_AGENT_ID.fullmatch(candidate) and candidate.casefold() not in seen:
            result.append(candidate)
            seen.add(candidate.casefold())
    return result


def _gateway_agents(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str)
        and _SAFE_AGENT_ID.fullmatch(key.strip())
        and isinstance(value, dict)
    }


def load_agent_catalog() -> dict[str, WarRoomAgent]:
    root = Path(__file__).resolve().parent
    openclaw_path = Path(
        os.environ.get(
            "PLACHEM_OPENCLAW_CONFIG",
            Path(os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))) / "openclaw.json",
        )
    ).expanduser()
    gateway_path = Path(
        os.environ.get("PLACHEM_FAST_GATEWAY_AGENTS", root / "plachem_fast_gateway" / "agents.json")
    ).expanduser()
    registered = _openclaw_agent_ids(openclaw_path)
    gateway = _gateway_agents(gateway_path)
    gateway_by_fold = {key.casefold(): value for key, value in gateway.items()}
    # Explicit isolated test mode may intentionally provide neither config.
    # Keep the historical fixtures usable without broadening production
    # execution admission.
    isolated_fixture = not registered and os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") == "1"
    if isolated_fixture:
        registered = ["main", "ERPcoder", "ERPmanager", "ERPqa"]

    display_by_fold = {
        value.casefold(): _LEGACY_DISPLAY.get(value.casefold(), value)
        for value in registered
    }
    catalog: dict[str, WarRoomAgent] = {}
    for raw_id in registered:
        folded = raw_id.casefold()
        agent_id = display_by_fold[folded]
        config = gateway_by_fold.get(folded)
        enabled = bool(config.get("enabled", True)) if config is not None else True
        # Execution admission is the strict intersection with the existing
        # Fast Gateway registry; registration alone never grants execution.
        allowed = bool(config.get("allowed", True)) if config is not None else False
        capabilities = config.get("capabilities", []) if config is not None else []
        safe_capabilities = tuple(
            value for value in capabilities if isinstance(value, str) and value.strip()
        ) if isinstance(capabilities, list) else ()
        catalog[agent_id] = WarRoomAgent(
            agent_id=agent_id,
            registered=True,
            gateway_allowed=allowed,
            enabled=enabled,
            capabilities=safe_capabilities,
        )
    return catalog


def canonical_agent_id(value: Any) -> str | None:
    if not isinstance(value, str) or not _SAFE_AGENT_ID.fullmatch(value.strip()):
        return None
    folded = value.strip().casefold()
    return next((key for key in load_agent_catalog() if key.casefold() == folded), None)


def catalog_items() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": item.agent_id,
            "registered": item.registered,
            "gateway_allowed": item.gateway_allowed,
            "enabled": item.enabled,
            "execution_eligible": item.execution_eligible,
            "capabilities": list(item.capabilities),
        }
        for item in sorted(load_agent_catalog().values(), key=lambda item: item.agent_id.casefold())
    ]
