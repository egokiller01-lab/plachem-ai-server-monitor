from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_query import RunQuery


@dataclass(frozen=True)
class WorkerSelection:
    agent_id: str
    reason: str
    availability: str


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
            if "runtime_profile" in config:
                profile = config["runtime_profile"]
                if not isinstance(profile, str) or not profile.strip():
                    raise ValueError(f"invalid runtime profile: {agent_id}")
                capabilities = config.get("capabilities", [])
                if not isinstance(capabilities, list) or not all(
                    isinstance(value, str) and value.strip() for value in capabilities
                ):
                    raise ValueError(f"invalid capabilities: {agent_id}")
                if not isinstance(config.get("enabled", True), bool):
                    raise ValueError(f"invalid enabled flag: {agent_id}")
                workspace_ids = config.get("workspace_ids", [])
                if not isinstance(workspace_ids, list) or not all(
                    isinstance(value, str) and value for value in workspace_ids
                ):
                    raise ValueError(f"invalid workspace ids: {agent_id}")
            else:
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

    def select_worker(self, task: dict[str, Any], run_query: RunQuery) -> WorkerSelection:
        required = task.get("required_capabilities", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("INVALID_REQUIRED_CAPABILITIES")
        required_set = {item.strip().lower() for item in required if item.strip()}
        preferred = task.get("preferred_worker", task.get("requested_worker"))
        if preferred is not None and not isinstance(preferred, str):
            raise ValueError("INVALID_PREFERRED_WORKER")
        mode = task.get("worker_selection_mode", "preferred")
        if mode not in {"preferred", "strict"}:
            raise ValueError("INVALID_WORKER_SELECTION_MODE")
        workspace_id = task.get("workspace_id")
        active_workers = {
            record.get("worker")
            for record in run_query.active()
            if isinstance(record.get("worker"), str)
        }

        ordered = list(self._agents.items())
        priority_index = {agent_id: index for index, (agent_id, _) in enumerate(ordered)}
        ordered.sort(key=lambda item: (item[1].get("priority", 0), priority_index[item[0]]))
        candidates: list[tuple[str, dict[str, Any], str]] = []
        for agent_id, config in ordered:
            if not config.get("runtime_profile"):
                continue
            if config.get("enabled", True) is not True:
                continue
            capabilities = {item.strip().lower() for item in config.get("capabilities", [])}
            if not required_set.issubset(capabilities):
                continue
            allowed_workspaces = config.get("workspace_ids", [])
            if allowed_workspaces and workspace_id not in allowed_workspaces:
                continue
            availability = "BUSY" if agent_id in active_workers else "AVAILABLE"
            if availability != "AVAILABLE":
                continue
            reason = "capability_match"
            candidates.append((agent_id, config, reason))

        if preferred:
            preferred_candidate = next((item for item in candidates if item[0] == preferred), None)
            if preferred_candidate:
                return WorkerSelection(preferred_candidate[0], "preferred_worker", "AVAILABLE")
            if mode == "strict":
                raise ValueError("NO_AVAILABLE_WORKER")
        if candidates:
            return WorkerSelection(candidates[0][0], "capability_fallback", "AVAILABLE")
        raise ValueError("NO_AVAILABLE_WORKER")
