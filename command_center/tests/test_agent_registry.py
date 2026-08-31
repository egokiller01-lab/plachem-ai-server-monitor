import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_registry import AgentRegistry


class AgentRegistryTests(unittest.TestCase):
    def write_registry(self, root: Path) -> Path:
        path = root / "agents.json"
        path.write_text(
            json.dumps(
                {
                    "achilles": {
                        "provider": "local-openai-compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "model": "local-model",
                    },
                    "athena": {
                        "provider": "hermes-profile",
                        "profile": "athena",
                        "model": "gpt-5.6-luna",
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_resolves_achilles_from_registry(self):
        with tempfile.TemporaryDirectory() as td:
            registry = AgentRegistry.load(self.write_registry(Path(td)))

            resolved = registry.resolve("achilles")

            self.assertEqual(resolved["provider"], "local-openai-compatible")
            self.assertEqual(resolved["model"], "local-model")
    def test_resolves_athena_provider_without_agent_specific_branch(self):
        with tempfile.TemporaryDirectory() as td:
            registry = AgentRegistry.load(self.write_registry(Path(td)))

            resolved = registry.resolve("athena")

            self.assertEqual(resolved["provider"], "hermes-profile")
            self.assertEqual(resolved["profile"], "athena")

    def test_unknown_agent_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            registry = AgentRegistry.load(self.write_registry(Path(td)))

            with self.assertRaisesRegex(ValueError, "^UNKNOWN_AGENT:missing$"):
                registry.resolve("missing")

    def test_rejects_non_object_config_and_invalid_provider(self):
        invalid_cases = [
            ({"broken": "not-an-object"}, "agent config must be an object: broken"),
            ({"broken": {}}, "invalid provider: broken"),
            ({"broken": {"provider": "   "}}, "invalid provider: broken"),
        ]
        for payload, reason in invalid_cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "agents.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, f"^{reason}$"):
                    AgentRegistry.load(path)

    def test_returns_full_registry_without_mutating_source_or_internal_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.write_registry(Path(td))
            before = path.read_bytes()
            registry = AgentRegistry.load(path)

            gateway_agents = registry.gateway_agents()
            gateway_agents["athena"]["provider"] = "changed"

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(registry.resolve("athena")["provider"], "hermes-profile")
            self.assertEqual(set(registry.gateway_agents()), {"achilles", "athena"})


if __name__ == "__main__":
    unittest.main()
