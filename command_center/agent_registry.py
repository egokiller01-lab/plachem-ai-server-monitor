from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


class AgentRegistry:
    def __init__(self, agents: dict[str, Any]) -> None:
        self._agents = copy.deepcopy(agents)

    @classmethod
    def load(cls, path: str | Path) -> "AgentRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("agent registry must contain one JSON object")
        for agent_id, config in data.items():
            if not isinstance(config, dict):
                raise ValueError(f"agent config must be an object: {agent_id}")
            provider = config.get("provider")
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError(f"invalid provider: {agent_id}")
        return cls(data)

    def resolve(self, agent_id: str) -> dict[str, Any]:
        if agent_id not in self._agents:
            raise ValueError(f"UNKNOWN_AGENT:{agent_id}")
        return copy.deepcopy(self._agents[agent_id])

    def gateway_agents(self) -> dict[str, Any]:
        return copy.deepcopy(self._agents)
