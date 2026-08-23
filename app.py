from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from war_room import router as war_room_router


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PLACHEM AI Server Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(war_room_router)

_network_prev: dict[str, float | int] | None = None


def pct(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def bytes_to_gb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**3), 1)


def safe_run(args: list[str], timeout: float = 1.5) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None


SENSITIVE_KEY_PATTERN = re.compile(r"(token|secret|api[-_]?key|password|credential|private[-_]?key)", re.I)

def openclaw_home() -> Path:
    return Path(os.getenv("OPENCLAW_HOME", str(Path.home() / ".openclaw"))).expanduser()


def read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                result[str(key)] = item if item is None or item == "" else "***REDACTED***"
            else:
                result[str(key)] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def normalize_model_ref(value: Any, fallback: str = "unknown") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("primary", "default", "id", "name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return fallback


def read_identity_value(paths: list[Path], label: str) -> str | None:
    pattern = re.compile(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", re.I)
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in content.splitlines():
            match = pattern.search(line)
            if match:
                value = match.group(1).strip().strip("_*` ")
                if value:
                    return value
    return None


def identity_paths(home: Path, agent_id: str, agent: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in ("agentDir", "workspace"):
        raw = agent.get(key)
        if isinstance(raw, str) and raw:
            paths.append(Path(raw).expanduser() / "IDENTITY.md")
    paths.extend([home / "agents" / agent_id / "agent" / "IDENTITY.md", home / f"workspace-{agent_id}" / "IDENTITY.md"])
    if agent_id == "main":
        paths.append(home / "workspace" / "IDENTITY.md")
    return paths


def openclaw_agent_ids(config: dict[str, Any], home: Path) -> list[str]:
    agents = config.get("agents", {}).get("list", [])
    ids = [str(agent.get("id")) for agent in agents if isinstance(agent, dict) and agent.get("id")]
    if ids:
        return ids
    try:
        ids = sorted(path.name for path in (home / "agents").iterdir() if path.is_dir() and not path.name.startswith("."))
    except Exception:
        ids = []
    return ids or ["main"]


def parse_session_type(key: str) -> str:
    markers = {":feishu:direct:": "feishu-dm", ":feishu:group:": "feishu-group", ":discord:direct:": "discord-dm", ":discord:channel:": "discord-channel", ":telegram:direct:": "telegram-dm", ":telegram:group:": "telegram-group", ":whatsapp:direct:": "whatsapp-dm", ":whatsapp:group:": "whatsapp-group", ":cron:": "cron"}
    if key.endswith(":main"):
        return "main"
    for marker, label in markers.items():
        if marker in key:
            return label
    return "unknown"


def read_agent_sessions(home: Path, agent_id: str) -> dict[str, Any]:
    sessions_dir = home / "agents" / agent_id / "sessions"
    raw = read_json_file(sessions_dir / "sessions.json")
    sessions = raw if isinstance(raw, dict) else {}
    rows: list[dict[str, Any]] = []
    last_active: int | None = None
    total_tokens = 0
    context_tokens = 0
    for key, item in sessions.items():
        if not isinstance(item, dict):
            continue
        updated = int(item.get("updatedAt") or 0)
        last_active = max(last_active or 0, updated) or None
        total_tokens += int(item.get("totalTokens") or 0)
        context_tokens += int(item.get("contextTokens") or 0)
        rows.append({"key": str(key), "type": parse_session_type(str(key)), "updated_at": updated, "total_tokens": int(item.get("totalTokens") or 0), "context_tokens": int(item.get("contextTokens") or 0), "system_sent": bool(item.get("systemSent", False))})
    last_assistant: int | None = None
    try:
        files = sorted([path for path in sessions_dir.iterdir() if path.name.endswith(".jsonl") and ".deleted." not in path.name], key=lambda path: path.stat().st_mtime, reverse=True)[:5]
        for path in files:
            if time.time() - path.stat().st_mtime > 180:
                continue
            for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]):
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("type") == "message" and entry.get("message", {}).get("role") == "assistant" and entry.get("timestamp"):
                    try:
                        ts = int(time.mktime(time.strptime(entry["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))) * 1000
                    except Exception:
                        continue
                    last_assistant = max(last_assistant or 0, ts)
                    last_active = max(last_active or 0, ts) or None
                    break
    except Exception:
        pass
    now_ms = int(time.time() * 1000)
    state = "offline"
    if last_active:
        diff = now_ms - last_active
        if last_assistant and now_ms - last_assistant < 180000:
            state = "working"
        elif diff < 600000:
            state = "online"
        elif diff < 86400000:
            state = "idle"
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return {"state": state, "last_active": last_active, "session_count": len(rows), "total_tokens": total_tokens, "context_tokens": context_tokens, "recent_sessions": redact_sensitive(rows[:8])}


def collect_openclaw_agents(config: dict[str, Any], home: Path) -> list[dict[str, Any]]:
    defaults = config.get("agents", {}).get("defaults", {})
    default_model = normalize_model_ref(defaults.get("model"), "unknown")
    configured = {str(agent.get("id")): agent for agent in config.get("agents", {}).get("list", []) if isinstance(agent, dict) and agent.get("id")}
    bindings = config.get("bindings", []) if isinstance(config.get("bindings"), list) else []
    channels = config.get("channels", {}) if isinstance(config.get("channels"), dict) else {}
    rows: list[dict[str, Any]] = []
    for agent_id in openclaw_agent_ids(config, home):
        agent = configured.get(agent_id, {"id": agent_id})
        paths = identity_paths(home, agent_id, agent)
        name = read_identity_value(paths, "Name") or agent.get("name") or agent.get("identity", {}).get("name") or agent_id
        emoji = read_identity_value(paths, "Emoji") or agent.get("identity", {}).get("emoji") or agent.get("emoji") or ""
        platforms: set[str] = set()
        for binding in bindings:
            if isinstance(binding, dict) and binding.get("agentId") == agent_id:
                channel = binding.get("match", {}).get("channel")
                if channel:
                    platforms.add(str(channel))
        if agent_id == "main":
            for channel, channel_config in channels.items():
                if isinstance(channel_config, dict) and channel_config.get("enabled") is not False:
                    platforms.add(str(channel))
        rows.append({"id": agent_id, "name": str(name), "emoji": str(emoji), "model": normalize_model_ref(agent.get("model"), default_model), "platforms": sorted(platforms), "session": read_agent_sessions(home, agent_id)})
    return rows


def collect_openclaw_providers(config: dict[str, Any], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    auth_profiles = config.get("auth", {}).get("profiles", {})
    auth_ids = {str(profile.get("provider") or key.split(":")[0]) for key, profile in auth_profiles.items() if isinstance(profile, dict)} if isinstance(auth_profiles, dict) else set()
    providers: dict[str, dict[str, Any]] = {}
    providers_config = config.get("models", {}).get("providers", {})
    if isinstance(providers_config, dict):
        for provider_id, provider in providers_config.items():
            provider = provider if isinstance(provider, dict) else {}
            models = []
            for model in provider.get("models", []) or []:
                if isinstance(model, dict):
                    models.append({"id": model.get("id") or model.get("name") or "unknown", "name": model.get("name") or model.get("id") or "unknown", "context_window": model.get("contextWindow"), "max_tokens": model.get("maxTokens"), "reasoning": model.get("reasoning")})
            providers[str(provider_id)] = {"id": str(provider_id), "api": provider.get("api"), "access_mode": "auth" if str(provider_id) in auth_ids else "api_key", "models": models, "used_by": []}
    def add_ref(ref: str | None) -> None:
        if not ref or "/" not in ref:
            return
        provider_id, model_id = ref.split("/", 1)
        provider = providers.setdefault(provider_id, {"id": provider_id, "api": None, "access_mode": "auth" if provider_id in auth_ids else "api_key", "models": [], "used_by": []})
        if not any(model.get("id") == model_id for model in provider["models"]):
            provider["models"].append({"id": model_id, "name": model_id, "context_window": None, "max_tokens": None, "reasoning": None})
    defaults = config.get("agents", {}).get("defaults", {})
    add_ref(normalize_model_ref(defaults.get("model"), ""))
    if isinstance(defaults.get("model"), dict):
        for fallback in defaults["model"].get("fallbacks", []) or []:
            add_ref(fallback)
    for agent in agents:
        add_ref(agent.get("model"))
    for provider in providers.values():
        provider["used_by"] = [{"id": agent["id"], "name": agent["name"], "emoji": agent["emoji"]} for agent in agents if isinstance(agent.get("model"), str) and agent["model"].startswith(provider["id"] + "/")]
    return sorted(providers.values(), key=lambda item: item["id"])


def parse_skill_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip()
    return result


def scan_openclaw_skills(directory: Path, source: str) -> list[dict[str, Any]]:
    try:
        children = sorted(path for path in directory.iterdir() if path.is_dir())
    except Exception:
        return []
    skills: list[dict[str, Any]] = []
    for child in children:
        skill_md = child / "SKILL.md"
        try:
            content = skill_md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        fm = parse_skill_frontmatter(content)
        skills.append({"id": child.name, "name": fm.get("name") or child.name, "description": fm.get("description") or "", "source": source, "location": str(skill_md)})
    return skills


def collect_openclaw_skills(config: dict[str, Any], home: Path) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    package_candidates = [os.getenv("OPENCLAW_PACKAGE_DIR"), str(Path.home() / ".local/lib/node_modules/openclaw"), "/usr/local/lib/node_modules/openclaw", "/usr/lib/node_modules/openclaw", "/opt/homebrew/lib/node_modules/openclaw"]
    package_dir = next((Path(path).expanduser() for path in package_candidates if path and (Path(path).expanduser() / "package.json").exists()), None)
    if package_dir:
        skills.extend(scan_openclaw_skills(package_dir / "skills", "builtin"))
    skills.extend(scan_openclaw_skills(home / "skills", "custom"))
    skills.extend(scan_openclaw_skills(home / "workspace" / "skills", "workspace:main"))
    for agent in config.get("agents", {}).get("list", []) or []:
        if isinstance(agent, dict):
            for key in ("workspace", "agentDir"):
                raw = agent.get(key)
                if isinstance(raw, str) and raw:
                    skills.extend(scan_openclaw_skills(Path(raw).expanduser() / "skills", "workspace:" + str(agent.get("id", "agent"))))
    by_source: dict[str, int] = {}
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for skill in skills:
        key = (skill["source"], skill["id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill)
        by_source[skill["source"]] = by_source.get(skill["source"], 0) + 1
    return {"total": len(unique), "by_source": by_source, "items": unique[:80]}


def probe_openclaw_gateway(config: dict[str, Any]) -> dict[str, Any]:
    gateway = config.get("gateway", {}) if isinstance(config.get("gateway"), dict) else {}
    port = int(gateway.get("port") or 18789)
    token = gateway.get("auth", {}).get("token") if isinstance(gateway.get("auth"), dict) else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.time()
    for path in ("/api/health", "/chat"):
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            with urllib.request.urlopen(request, timeout=2.5) as response:
                response_ms = round((time.time() - started) * 1000)
                return {"state": "degraded" if response_ms > 1500 else "healthy", "ok": True, "port": port, "response_ms": response_ms, "checked_path": path, "http_status": response.status}
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return {"state": "degraded", "ok": True, "port": port, "response_ms": round((time.time() - started) * 1000), "checked_path": path, "http_status": exc.code}
        except Exception:
            continue
    return {"state": "down", "ok": False, "port": port, "response_ms": round((time.time() - started) * 1000)}


def collect_openclaw_status() -> dict[str, Any]:
    home = openclaw_home()
    config_path = home / "openclaw.json"
    base = {"timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), "home": str(home), "config_path": str(config_path)}
    empty = {"summary": {"connection": "Missing config", "agent_count": 0, "model_count": 0, "provider_count": 0, "skill_count": 0, "gateway": "unknown"}, "agents": [], "providers": [], "skills": {"total": 0, "by_source": {}, "items": []}, "gateway": {"state": "unknown", "ok": False}, "raw": {}}
    if not config_path.exists():
        return {**base, **empty, "status": "not_configured"}
    config = read_json_file(config_path)
    if not isinstance(config, dict):
        return {**base, **empty, "status": "error", "summary": {**empty["summary"], "connection": "Invalid config"}}
    agents = collect_openclaw_agents(config, home)
    providers = collect_openclaw_providers(config, agents)
    skills = collect_openclaw_skills(config, home)
    gateway = probe_openclaw_gateway(config)
    model_count = sum(len(provider.get("models", [])) for provider in providers)
    payload = {**base, "status": "ok", "config_modified": int(config_path.stat().st_mtime), "summary": {"connection": "Connected", "agent_count": len(agents), "model_count": model_count, "provider_count": len(providers), "skill_count": skills["total"], "gateway": gateway["state"]}, "agents": agents, "providers": providers, "skills": skills, "gateway": gateway}
    payload["raw"] = redact_sensitive({"config": config, "agents": agents, "providers": providers, "skills": skills, "gateway": gateway})
    return payload


def get_cpu() -> dict[str, Any]:
    try:
        temperature_c = None
        temperature_source = None
        try:
            temperatures = psutil.sensors_temperatures(fahrenheit=False)
        except Exception:
            temperatures = {}
        for chip, label in (("k10temp", "Tctl"), ("coretemp", "Package id 0")):
            sensor = next((entry for entry in temperatures.get(chip, []) if entry.label == label), None)
            if sensor and isinstance(sensor.current, (int, float)):
                temperature_c = round(sensor.current, 1)
                temperature_source = f"{chip}:{label}"
                break
        return {
            "usage_percent": pct(psutil.cpu_percent(interval=None)),
            "temperature_c": temperature_c,
            "temperature_source": temperature_source,
            "status": "ok",
        }
    except Exception as exc:
        return {"usage_percent": None, "temperature_c": None, "temperature_source": None, "status": "error", "error": str(exc)}


def get_memory() -> dict[str, Any]:
    try:
        mem = psutil.virtual_memory()
        return {
            "used_gb": bytes_to_gb(mem.used),
            "total_gb": bytes_to_gb(mem.total),
            "usage_percent": pct(mem.percent),
            "status": "ok",
        }
    except Exception as exc:
        return {"used_gb": None, "total_gb": None, "usage_percent": None, "status": "error", "error": str(exc)}


def get_disk() -> dict[str, Any]:
    try:
        disk = psutil.disk_usage("/")
        return {
            "used_gb": bytes_to_gb(disk.used),
            "total_gb": bytes_to_gb(disk.total),
            "usage_percent": pct(disk.percent),
            "status": "ok",
        }
    except Exception as exc:
        return {"used_gb": None, "total_gb": None, "usage_percent": None, "status": "error", "error": str(exc)}


def get_network() -> dict[str, Any]:
    global _network_prev
    try:
        counters = psutil.net_io_counters()
        now = time.time()
        if _network_prev is None:
            _network_prev = {"time": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
            return {"upload_bps": 0, "download_bps": 0, "status": "warming"}

        elapsed = max(now - float(_network_prev["time"]), 0.001)
        upload = max(counters.bytes_sent - int(_network_prev["sent"]), 0) / elapsed
        download = max(counters.bytes_recv - int(_network_prev["recv"]), 0) / elapsed
        _network_prev = {"time": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
        return {"upload_bps": round(upload, 1), "download_bps": round(download, 1), "status": "ok"}
    except Exception as exc:
        return {"upload_bps": None, "download_bps": None, "status": "error", "error": str(exc)}


def _gpu_metrics_from_nvidia_smi_row(row: list[str], index: int) -> dict[str, Any] | None:
    name, gpu_util, mem_used, mem_total, temp = row[:5]
    uuid = row[5] if len(row) > 5 and row[5] else None
    try:
        used = float(mem_used)
        total = float(mem_total)
    except Exception:
        return None
    used_gb = round(used / 1024, 1)
    total_gb = round(total / 1024, 1)
    vram_percent = round((used / total) * 100, 1) if total else None
    power_draw = pct(row[6]) if len(row) > 6 else None
    power_limit = pct(row[7]) if len(row) > 7 else None
    fan_speed = pct(row[8]) if len(row) > 8 else None
    pci_bus_id = row[9] if len(row) > 9 and row[9] else None
    pcie_gen_current = row[10] if len(row) > 10 and row[10].isdigit() else None
    pcie_gen_max = row[11] if len(row) > 11 and row[11].isdigit() else None
    pcie_width_current = row[12] if len(row) > 12 and row[12].isdigit() else None
    pcie_width_max = row[13] if len(row) > 13 and row[13].isdigit() else None
    return {
        "index": index,
        "uuid": uuid,
        "name": name,
        "usage_percent": pct(gpu_util),
        "vram_used_gb": used_gb,
        "vram_total_gb": total_gb,
        "vram_usage_percent": vram_percent,
        "temperature_c": pct(temp),
        "power_draw_w": power_draw,
        "power_limit_w": power_limit,
        "fan_speed_percent": fan_speed,
        "pci_bus_id": pci_bus_id,
        "pcie_gen_current": int(pcie_gen_current) if pcie_gen_current is not None else None,
        "pcie_gen_max": int(pcie_gen_max) if pcie_gen_max is not None else None,
        "pcie_width_current": int(pcie_width_current) if pcie_width_current is not None else None,
        "pcie_width_max": int(pcie_width_max) if pcie_width_max is not None else None,
        "status": "ok",
        "source": "nvidia-smi",
    }


def get_gpus_from_nvidia_smi() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,uuid,power.draw,power.limit,fan.speed,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max"
    result = safe_run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(result.stdout.strip().splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "nvidia-smi",
                "error": "invalid nvidia-smi row",
            })
            continue
        metrics = _gpu_metrics_from_nvidia_smi_row(parts, i)
        if metrics is None:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "nvidia-smi",
                "error": "parse failure",
            })
        else:
            out.append(metrics)
    return out


def get_gpus_from_pynvml() -> list[dict[str, Any]]:
    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception:
        return []
    count = 0
    out: list[dict[str, Any]] = []
    try:
        count = pynvml.nvmlDeviceGetCount()
    except Exception:
        count = 0
    for i in range(count):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            raw_name = pynvml.nvmlDeviceGetName(handle)
            name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
            uuid = pynvml.nvmlDeviceGetUUID(handle)
            uuid = uuid.decode("utf-8") if isinstance(uuid, bytes) else str(uuid)
        except Exception as exc:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "pynvml",
                "error": str(exc),
            })
            continue
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            util_gpu = util.gpu
        except Exception:
            util_gpu = None
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            mem_used = mem.used
            mem_total = mem.total
        except Exception:
            mem_used = None
            mem_total = None
        try:
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        except Exception:
            temp = None
        try:
            power_draw_w = pct(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)
        except Exception:
            power_draw_w = None
        try:
            power_limit_w = pct(pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0)
        except Exception:
            power_limit_w = None
        try:
            fan_speed = pct(pynvml.nvmlDeviceGetFanSpeed(handle))
        except Exception:
            fan_speed = None
        try:
            pci = pynvml.nvmlDeviceGetPciInfo(handle)
            pci_bus_id = f"{pci.domain:08X}:{pci.bus:02X}:{pci.device:02X}.0"
        except Exception:
            pci_bus_id = None
        try:
            pcie_gen_current = int(pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle))
        except Exception:
            pcie_gen_current = None
        try:
            pcie_gen_max = int(pynvml.nvmlDeviceGetMaxPcieLinkGeneration(handle))
        except Exception:
            pcie_gen_max = None
        try:
            pcie_width_current = int(pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle))
        except Exception:
            pcie_width_current = None
        try:
            pcie_width_max = int(pynvml.nvmlDeviceGetMaxPcieLinkWidth(handle))
        except Exception:
            pcie_width_max = None
        out.append({
            "index": i,
            "uuid": uuid,
            "name": name,
            "usage_percent": util_gpu,
            "vram_used_gb": bytes_to_gb(mem_used),
            "vram_total_gb": bytes_to_gb(mem_total),
            "vram_usage_percent": pct((mem_used / mem_total) * 100 if mem_total else None),
            "temperature_c": temp,
            "power_draw_w": power_draw_w,
            "power_limit_w": power_limit_w,
            "fan_speed_percent": fan_speed,
            "pci_bus_id": pci_bus_id,
            "pcie_gen_current": pcie_gen_current,
            "pcie_gen_max": pcie_gen_max,
            "pcie_width_current": pcie_width_current,
            "pcie_width_max": pcie_width_max,
            "status": "ok",
            "source": "pynvml",
        })
    try:
        pynvml.nvmlShutdown()
    except Exception:
        pass
    return out


def get_gpus() -> list[dict[str, Any]]:
    try:
        gpus = get_gpus_from_nvidia_smi() or get_gpus_from_pynvml()
        return gpus if gpus else [{
            "index": 0,
            "uuid": None,
            "name": "Unknown",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "power_draw_w": None,
            "power_limit_w": None,
            "fan_speed_percent": None,
            "pci_bus_id": None,
            "pcie_gen_current": None,
            "pcie_gen_max": None,
            "pcie_width_current": None,
            "pcie_width_max": None,
            "status": "unknown",
            "source": "none",
        }]
    except Exception as exc:
        return [{
            "index": 0,
            "uuid": None,
            "name": "Error",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "power_draw_w": None,
            "power_limit_w": None,
            "fan_speed_percent": None,
            "pci_bus_id": None,
            "pcie_gen_current": None,
            "pcie_gen_max": None,
            "pcie_width_current": None,
            "pcie_width_max": None,
            "status": "error",
            "source": "none",
            "error": str(exc),
        }]


def get_gpu() -> dict[str, Any]:
    gpus = get_gpus()
    return gpus[0] if gpus else {
        "uuid": None,
        "name": "Unknown",
        "usage_percent": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_usage_percent": None,
        "temperature_c": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "fan_speed_percent": None,
        "pci_bus_id": None,
        "pcie_gen_current": None,
        "pcie_gen_max": None,
        "pcie_width_current": None,
        "pcie_width_max": None,
        "status": "unknown",
        "source": "none",
    }


def systemctl_status(service: str) -> str | None:
    if not shutil.which("systemctl"):
        return None
    result = safe_run(["systemctl", "is-active", service], timeout=1.0)
    if not result:
        return None
    state = result.stdout.strip()
    if state in {"active", "inactive", "failed", "activating", "deactivating"}:
        return state
    return None


def process_running(names: list[str]) -> bool:
    wanted = {name.lower() for name in names}
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if any(item in name or item in cmdline for item in wanted):
                return True
    except Exception:
        return False
    return False


def service_status(service: str, process_names: list[str]) -> dict[str, str]:
    try:
        state = systemctl_status(service)
        if state == "active":
            return {"state": "running", "label": "Running"}
        if process_running(process_names):
            return {"state": "running", "label": "Running"}
        if state in {"inactive", "failed"}:
            return {"state": "stopped", "label": "Stopped"}
        return {"state": "unknown", "label": "Unknown"}
    except Exception:
        return {"state": "error", "label": "Error"}


def get_services() -> dict[str, dict[str, str]]:
    return {
        "openclaw": service_status("openclaw", ["openclaw"]),
        "ollama": service_status("ollama", ["ollama"]),
        "tailscale": service_status("tailscaled", ["tailscaled", "tailscale"]),
    }


def get_uptime() -> dict[str, Any]:
    try:
        seconds = int(time.time() - psutil.boot_time())
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        return {"seconds": seconds, "label": f"{days}d {hours}h {minutes}m", "status": "ok"}
    except Exception:
        return {"seconds": None, "label": "Unknown", "status": "error"}


def mb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**2), 1)


def gb(value: float | int | None) -> float | None:
    return bytes_to_gb(value)


def process_name(pid: int | None) -> str | None:
    if pid is None:
        return None
    try:
        return psutil.Process(pid).name()
    except Exception:
        return None


def top_processes(kind: str, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    attrs = ["pid", "name", "cpu_percent", "memory_percent", "memory_info"]
    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            mem_info = info.get("memory_info")
            rows.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "Unknown",
                "cpu_percent": pct(info.get("cpu_percent") or 0),
                "memory_percent": pct(info.get("memory_percent") or 0),
                "rss_mb": mb(mem_info.rss if mem_info else None),
            })
        except Exception:
            continue
    key = "cpu_percent" if kind == "cpu" else "rss_mb"
    return sorted(rows, key=lambda item: item.get(key) or 0, reverse=True)[:limit]


def disk_partitions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            rows.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": gb(usage.total),
                "used_gb": gb(usage.used),
                "free_gb": gb(usage.free),
                "usage_percent": pct(usage.percent),
            })
        except Exception:
            continue
    return rows


def folder_size(path: str) -> dict[str, Any]:
    result = safe_run(["du", "-sb", path], timeout=1.2)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return {"path": path, "size_gb": None, "status": "unknown"}
    try:
        size = int(result.stdout.split()[0])
        return {"path": path, "size_gb": gb(size), "status": "ok"}
    except Exception:
        return {"path": path, "size_gb": None, "status": "error"}


def major_folder_sizes() -> list[dict[str, Any]]:
    raw_folders = os.getenv("PLACHEM_MONITOR_FOLDERS", "/tmp")
    candidates = [item.strip() for item in raw_folders.split(",") if item.strip()]
    rows = [folder_size(path) for path in candidates if Path(path).exists()]
    return sorted(rows, key=lambda item: item.get("size_gb") or 0, reverse=True)


def gpu_processes() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "gpu_bus_id,gpu_uuid,pid,process_name,used_memory"
    result = safe_run(["nvidia-smi", f"--query-compute-apps={query}", "--format=csv,noheader,nounits"], timeout=1.5)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    gpus = get_gpus()
    uuid_to_index: dict[str, int] = {}
    bus_to_index: dict[str, int] = {}
    for g in gpus:
        if g.get("uuid"):
            uuid_to_index[str(g["uuid"])] = g["index"]
        if g.get("pci_bus_id"):
            bus_to_index[str(g["pci_bus_id"])] = g["index"]
    rows: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            bus_id, uuid, pid, name, vram = parts[0], parts[1], int(parts[2]), parts[3], float(parts[4])
        except Exception:
            continue
        gpu_index = uuid_to_index.get(uuid) if uuid else None
        if gpu_index is None:
            gpu_index = bus_to_index.get(bus_id) if bus_id else None
        rows.append({
            "gpu_index": gpu_index,
            "gpu_uuid": uuid if uuid else None,
            "gpu_bus_id": bus_id if bus_id else None,
            "pid": pid,
            "name": name,
            "vram_mb": vram,
        })
    return sorted(rows, key=lambda item: item["vram_mb"], reverse=True)


LLAMA_VALUE_FLAGS = {
    "-m": "model",
    "--model": "model",
    "-c": "context_size",
    "--ctx-size": "context_size",
    "-ngl": "gpu_layers",
    "--n-gpu-layers": "gpu_layers",
    "--tensor-split": "tensor_split",
    "--host": "host",
    "-H": "host",
    "--port": "port",
    "-p": "port",
}

LLAMA_BOOL_FLAGS = {"--metrics", "--help", "-h"}


def _parse_llama_args(args: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in LLAMA_BOOL_FLAGS:
            i += 1
            continue
        if arg in LLAMA_VALUE_FLAGS:
            if i + 1 < len(args):
                out[LLAMA_VALUE_FLAGS[arg]] = args[i + 1]
                i += 2
                continue
        i += 1
    return out


def _llama_health_probe(host: str | None, port: int | None) -> dict[str, Any] | None:
    if not host or not port:
        return None
    url = f"http://{host}:{port}/health"
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            status = resp.status
            resp.read()
        return {"ok": status == 200, "http_status": status, "response_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "http_status": None, "response_ms": int((time.time() - started) * 1000), "error": str(exc)}


def _llama_gpu_usage(pid: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in gpu_processes():
        if item.get("pid") == pid:
            rows.append({"gpu_index": item.get("gpu_index"), "gpu_uuid": item.get("gpu_uuid"), "vram_mb": item.get("vram_mb")})
    return rows


def detect_llama_server() -> dict[str, Any]:
    now = time.time()
    for proc in psutil.process_iter(["pid", "exe", "cmdline", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            exe = info.get("exe") or ""
            cmd = info.get("cmdline") or []
            base = exe.rsplit("/", 1)[-1]
            if "llama-server" not in base and not any("llama-server" in a for a in cmd):
                continue
            args = _parse_llama_args(cmd)
            model_path = args.get("model")
            model = model_path.rsplit("/", 1)[-1] if model_path else None
            gpu_layers = args.get("gpu_layers")
            if isinstance(gpu_layers, str) and gpu_layers.isdigit():
                gpu_layers = int(gpu_layers)
            tensor_split = args.get("tensor_split")
            if isinstance(tensor_split, str):
                try:
                    tensor_split = [float(x) for x in tensor_split.split(",") if x]
                except Exception:
                    tensor_split = None
            port = args.get("port")
            if isinstance(port, str) and port.isdigit():
                port = int(port)
            try:
                rss_mb = round(info["memory_info"].rss / 1024 / 1024, 1)
            except Exception:
                rss_mb = None
            runtime = {
                "running": True,
                "pid": info["pid"],
                "process_name": base if base else None,
                "executable": exe or None,
                "model": model,
                "model_path": model_path,
                "port": port,
                "host": args.get("host"),
                "context_size": int(args["context_size"]) if isinstance(args.get("context_size"), str) and args["context_size"].isdigit() else None,
                "gpu_layers": gpu_layers,
                "tensor_split": tensor_split,
                "uptime_seconds": int(now - psutil.Process(info["pid"]).create_time()),
                "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                "memory_rss_mb": rss_mb,
                "gpus": _llama_gpu_usage(info["pid"]),
                "health": _llama_health_probe(args.get("host"), port),
            }
            return runtime
        except Exception:
            continue
    return {"running": False}


def network_interfaces() -> list[dict[str, Any]]:
    counters = psutil.net_io_counters(pernic=True)
    stats = psutil.net_if_stats()
    rows: list[dict[str, Any]] = []
    for name, item in counters.items():
        stat = stats.get(name)
        rows.append({
            "name": name,
            "is_up": bool(stat.isup) if stat else None,
            "speed_mbps": stat.speed if stat else None,
            "sent_mb": mb(item.bytes_sent),
            "recv_mb": mb(item.bytes_recv),
            "packets_sent": item.packets_sent,
            "packets_recv": item.packets_recv,
            "errin": item.errin,
            "errout": item.errout,
            "dropin": item.dropin,
            "dropout": item.dropout,
        })
    return sorted(rows, key=lambda row: (not bool(row.get("is_up")), row["name"]))


def listening_ports(limit: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            ip = conn.laddr.ip
            port = conn.laddr.port
            rows.append({"ip": ip, "port": port, "pid": conn.pid, "process": process_name(conn.pid) or "Unknown"})
    except Exception:
        return []
    return sorted(rows, key=lambda row: (row["port"], row["ip"]))[:limit]


def extended_services() -> dict[str, dict[str, str]]:
    services = {
        "openclaw": ("openclaw", ["openclaw"]),
        "ollama": ("ollama", ["ollama"]),
        "tailscale": ("tailscaled", ["tailscaled", "tailscale"]),
        "open_webui": ("open-webui", ["open-webui", "openwebui"]),
        "postiz": ("postiz", ["postiz"]),
        "paperclip": ("paperclip", ["paperclip"]),
        "server_monitor": ("plachem-ai-server-monitor", ["uvicorn", "plachem-ai-server-monitor"]),
    }
    return {key: service_status(service, names) for key, (service, names) in services.items()}


def detail_response(name: str, payload: dict[str, Any], status: str = "ok") -> dict[str, Any]:
    return {"name": name, "status": status, "timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), **payload}


@app.get("/api/detail/cpu")
def detail_cpu() -> dict[str, Any]:
    try:
        return detail_response("cpu", {
            "summary": get_cpu(),
            "core_percent": [pct(value) for value in psutil.cpu_percent(interval=0.1, percpu=True)],
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "top_processes": top_processes("cpu"),
        })
    except Exception as exc:
        return detail_response("cpu", {"error": str(exc)}, "error")


@app.get("/api/detail/memory")
def detail_memory() -> dict[str, Any]:
    try:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return detail_response("memory", {
            "summary": get_memory(),
            "memory": {
                "total_gb": gb(mem.total),
                "used_gb": gb(mem.used),
                "free_gb": gb(mem.free),
                "available_gb": gb(mem.available),
                "cached_gb": gb(getattr(mem, "cached", 0)),
                "buffers_gb": gb(getattr(mem, "buffers", 0)),
                "usage_percent": pct(mem.percent),
            },
            "swap": {
                "total_gb": gb(swap.total),
                "used_gb": gb(swap.used),
                "free_gb": gb(swap.free),
                "usage_percent": pct(swap.percent),
            },
            "top_processes": top_processes("memory"),
        })
    except Exception as exc:
        return detail_response("memory", {"error": str(exc)}, "error")


@app.get("/api/detail/gpu")
def detail_gpu() -> dict[str, Any]:
    try:
        return detail_response("gpu", {"summary": get_gpu(), "processes": gpu_processes()})
    except Exception as exc:
        return detail_response("gpu", {"error": str(exc)}, "error")


@app.get("/api/detail/llama")
def detail_llama() -> dict[str, Any]:
    try:
        return detail_response("llama", {"runtime": detect_llama_server()})
    except Exception as exc:
        return detail_response("llama", {"runtime": {"running": False}}, "error")


def _clamp_percent(value: Any) -> int | None:
    if value is None:
        return None
    try:
        v = int(round(float(value)))
    except Exception:
        return None
    return max(0, min(100, v))


def _sanitize_error(exc: Exception) -> str:
    try:
        msg = str(exc)
    except Exception:
        msg = "internal error"
    msg = msg[:200]
    msg = re.sub(r"(?i)(token|secret|api[_-]?key|password|credential|private[_-]?key|authorization|cookie|oauth)[^,\s}]*",
                 "[REDACTED]", msg)
    return msg or "unknown error"


def _whitelist_bucket(bucket: Any) -> dict[str, Any] | None:
    if not isinstance(bucket, dict):
        return None
    used = bucket.get("usedPercent")
    resets = bucket.get("resetsAt")
    window = bucket.get("windowDurationMins")
    remaining = _clamp_percent(100 - used if isinstance(used, (int, float)) else None)
    return {
        "used_percent": used if isinstance(used, (int, float)) else None,
        "remaining_percent": remaining,
        "resets_at": resets if isinstance(resets, (int, float)) else None,
        "window_duration_mins": window if isinstance(window, (int, float)) else None,
    }


def _collect_codex_rate_limits(timeout: float = 12.0) -> dict[str, Any]:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise RuntimeError("codex CLI not found on PATH")

    import subprocess as _sp
    import select as _sel
    import os as _os

    proc = _sp.Popen(
        [codex_bin, "app-server", "--listen", "stdio://"],
        stdin=_sp.PIPE,
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
    )

    def _send(payload: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()

    def _read(timeout: float) -> dict[str, Any]:
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            ready, _, _ = _sel.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = _os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                # Drain all available lines, return the last JSON-RPC response
                result: dict[str, Any] = {}
                while b"\n" in buf:
                    idx = buf.index(b"\n")
                    line = buf[:idx].decode("utf-8", errors="ignore").strip()
                    buf = buf[idx+1:]
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "id" in obj:
                            result = obj
                        elif isinstance(obj, dict) and obj.get("id") is not None:
                            result = obj
                    except Exception:
                        pass
                if result:
                    return result
        return {}

    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"clientInfo": {"name": "plachem-monitor", "version": "1.0.0"}}})
        init = _read(8)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init.get('error', {}).get('message', 'unknown')}")

        _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        resp = _read(8)
        if "error" in resp:
            raise RuntimeError(f"rateLimits/read failed: {resp.get('error', {}).get('message', 'unknown')}")

        result = resp.get("result", {})
        rate_limits = result.get("rateLimits", {})
        rate_limits_by_id = result.get("rateLimitsByLimitId", {})

        weekly = {
            "limit_id": rate_limits.get("limitId"),
            "used_percent": None,
            "remaining_percent": None,
            "resets_at": None,
            "window_duration_mins": None,
        }
        primary = rate_limits.get("primary") or {}
        if isinstance(primary, dict):
            weekly.update(_whitelist_bucket(primary))

        spark: dict[str, Any] = {
            "limit_name": None,
            "primary": None,
            "secondary": None,
        }
        bengalfox = rate_limits_by_id.get("codex_bengalfox") or {}
        if isinstance(bengalfox, dict):
            spark["limit_name"] = bengalfox.get("limitName")
            spark["primary"] = _whitelist_bucket(bengalfox.get("primary"))
            spark["secondary"] = _whitelist_bucket(bengalfox.get("secondary"))

        return {"weekly": weekly, "spark": spark}
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


@app.get("/api/detail/codex")
def detail_codex() -> dict[str, Any]:
    try:
        data = _collect_codex_rate_limits()
        return detail_response("codex", {
            "codex": {
                "weekly": data["weekly"],
            },
            "spark": {
                "limit_name": data["spark"].get("limit_name"),
                "primary": data["spark"].get("primary"),
                "secondary": data["spark"].get("secondary"),
            },
        })
    except Exception as exc:
        return detail_response("codex", {"codex": None, "spark": None, "error": _sanitize_error(exc)}, "error")


@app.get("/api/detail/disk")
def detail_disk() -> dict[str, Any]:
    try:
        return detail_response("disk", {"summary": get_disk(), "partitions": disk_partitions(), "major_folders": major_folder_sizes()})
    except Exception as exc:
        return detail_response("disk", {"error": str(exc)}, "error")


@app.get("/api/detail/network")
def detail_network() -> dict[str, Any]:
    try:
        return detail_response("network", {
            "summary": get_network(),
            "interfaces": network_interfaces(),
            "listening_ports": listening_ports(),
            "tailscale": service_status("tailscaled", ["tailscaled", "tailscale"]),
        })
    except Exception as exc:
        return detail_response("network", {"error": str(exc)}, "error")


@app.get("/api/openclaw/status")
def api_openclaw_status() -> dict[str, Any]:
    try:
        return collect_openclaw_status()
    except Exception as exc:
        return {"status": "error", "timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), "error": str(exc), "summary": {"connection": "Error", "agent_count": 0, "model_count": 0, "provider_count": 0, "skill_count": 0, "gateway": "unknown"}, "agents": [], "providers": [], "skills": {"total": 0, "by_source": {}, "items": []}, "gateway": {"state": "unknown", "ok": False}, "raw": {}}


@app.get("/api/detail/services")
def detail_services() -> dict[str, Any]:
    try:
        return detail_response("services", {"services": extended_services()})
    except Exception as exc:
        return detail_response("services", {"error": str(exc)}, "error")


@app.get("/compact")
def compact_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/openclaw")
def openclaw_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/openclaw-control")
def openclaw_control_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/war-room")
def war_room_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "war-room.html")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    cpu = get_cpu()
    memory = get_memory()
    disk = get_disk()
    network = get_network()
    gpus = get_gpus()
    gpu = gpus[0] if gpus else {
        "uuid": None,
        "name": "Unknown",
        "usage_percent": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_usage_percent": None,
        "temperature_c": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "fan_speed_percent": None,
        "pci_bus_id": None,
        "pcie_gen_current": None,
        "pcie_gen_max": None,
        "pcie_width_current": None,
        "pcie_width_max": None,
        "status": "unknown",
        "source": "none",
    }
    services = get_services()

    service_states = [item["state"] for item in services.values()]
    has_error = any(
        item.get("status") == "error" for item in [cpu, memory, disk, network, gpu]
    ) or "error" in service_states
    has_warning = any(item.get("status") in {"unknown", "warming"} for item in [network, gpu]) or any(
        state in {"stopped", "unknown"} for state in service_states
    )

    overall = "error" if has_error else "warning" if has_warning else "normal"

    return {
        "timestamp": int(time.time()),
        "last_updated": time.strftime("%H:%M:%S"),
        "server": {
            "name": os.getenv("PLACHEM_MONITOR_SERVER_NAME", "PLACHEM-AI-01"),
            "os": f"{platform.system()} {platform.release()}",
            "uptime": get_uptime(),
        },
        "overall": overall,
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "network": network,
        "gpu": gpu,
        "gpus": gpus,
        "services": services,
    }


def _is_automated_session_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("heartbeat", "smoke", "test", "benchmark", ":explicit:"))


def _select_erpmanager_session(sessions: dict[str, Any]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    candidates: list[tuple[tuple[int, int, int], str, dict[str, Any]]] = []
    for key, item in sessions.items():
        if not isinstance(item, dict) or item.get("status") == "failed":
            continue
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        is_webchat_direct = (
            origin.get("provider") == "webchat"
            and origin.get("surface") == "webchat"
            and (origin.get("chatType") == "direct" or item.get("chatType") == "direct")
        )
        is_dashboard = ":dashboard:" in str(key)
        if _is_automated_session_key(str(key)):
            continue
        status_active = item.get("status") in {"running", "working", "active"}
        priority = (
            2 if is_webchat_direct and is_dashboard else 1 if is_webchat_direct else 0,
            1 if status_active else 0,
            int(item.get("updatedAt") or 0),
        )
        candidates.append((priority, str(key), item))
    if not candidates:
        return None, None
    _, key, item = max(candidates, key=lambda candidate: candidate[0])
    return key, item


def _collect_agent_context(agent_id: str) -> dict[str, Any]:
    sessions = read_json_file(openclaw_home() / "agents" / agent_id / "sessions" / "sessions.json")
    if not isinstance(sessions, dict):
        raise RuntimeError("sessions.json not found or invalid")
    best_key, best_item, best_updated = None, None, 0
    if agent_id == "erpmanager":
        best_key, best_item = _select_erpmanager_session(sessions)
    else:
        for key, item in sessions.items():
            if not isinstance(item, dict):
                continue
            updated = int(item.get("updatedAt") or 0)
            if updated >= best_updated:
                best_key, best_item, best_updated = key, item, updated
    if best_item is None:
        raise RuntimeError("no active session found")
    total_tokens = best_item.get("totalTokens")
    context_tokens = best_item.get("contextTokens")
    input_tokens = best_item.get("inputTokens")
    output_tokens = best_item.get("outputTokens")
    cache_read = best_item.get("cacheRead")
    cache_write = best_item.get("cacheWrite")
    budget = best_item.get("contextBudgetStatus")
    estimate_source = None
    if isinstance(budget, dict) and isinstance(budget.get("estimatedPromptTokens"), (int, float)):
        budget = dict(budget)
        estimate_source = str(budget.get("source") or "pre-prompt-estimate")
    else:
        budget = _read_prompt_budget_from_gateway_journal(
            agent_id=agent_id,
            session_key=str(best_key),
            session_id=str(best_item.get("sessionId") or ""),
            context_token_budget=context_tokens,
        )
        estimate_source = "gateway-journal" if budget else None

    estimated_prompt_tokens = budget.get("estimatedPromptTokens") if budget else None
    context_token_budget = budget.get("contextTokenBudget") if budget else context_tokens
    prompt_budget = budget.get("promptBudgetBeforeReserve") if budget else None
    reserve_tokens = budget.get("reserveTokens") if budget else None
    remaining_prompt_budget_tokens = budget.get("remainingPromptBudgetTokens") if budget else None
    overflow_tokens = budget.get("overflowTokens") if budget else None
    route = budget.get("route") if budget else None
    prompt_percent = round(estimated_prompt_tokens / prompt_budget * 100) if isinstance(estimated_prompt_tokens, (int, float)) and isinstance(prompt_budget, (int, float)) and prompt_budget else None
    denom = (cache_read or 0) + (cache_write or 0) + (input_tokens or 0)
    cache_hit_percent = round(cache_read / denom * 100) if cache_read and denom else None
    if prompt_percent is None:
        health = None
    elif isinstance(overflow_tokens, (int, float)) and overflow_tokens > 0:
        health = "overflow"
    elif prompt_percent < 70:
        health = "normal"
    elif prompt_percent < 85:
        health = "warning"
    else:
        health = "critical"
    started = best_item.get("sessionStartedAt")
    now_ms = int(time.time() * 1000)
    duration_seconds = round((now_ms - started) / 1000) if isinstance(started, (int, float)) else None
    return {
        "agent_id": agent_id,
        "session_key": best_key,
        "session_id": best_item.get("sessionId"),
        "model": best_item.get("model"),
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "context_percent": prompt_percent,
        "prompt_percent": prompt_percent,
        "estimate_source": estimate_source,
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "context_token_budget": context_token_budget,
        "prompt_budget_before_reserve": prompt_budget,
        "reserve_tokens": reserve_tokens,
        "remaining_prompt_budget_tokens": remaining_prompt_budget_tokens,
        "overflow_tokens": overflow_tokens,
        "route": route,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cache_hit_percent": cache_hit_percent,
        "session_started_at": started,
        "updated_at": best_item.get("updatedAt"),
        "duration_seconds": duration_seconds,
        "health": health,
    }


_PROMPT_BUDGET_LOG_RE = re.compile(
    r"\[context-overflow-precheck\].*?sessionKey=(?P<session_key>\S+).*?"
    r"route=(?P<route>\S+).*?estimatedPromptTokens=(?P<estimated>\d+).*?"
    r"promptBudgetBeforeReserve=(?P<prompt_budget>\d+).*?overflowTokens=(?P<overflow>\d+).*?"
    r"reserveTokens=(?P<reserve>\d+)"
)


def _read_prompt_budget_from_gateway_journal(
    agent_id: str,
    session_key: str,
    session_id: str,
    context_token_budget: Any,
) -> dict[str, Any] | None:
    result = safe_run(
        ["journalctl", "--user", "-u", "openclaw-gateway", "--no-pager", "-o", "cat", "-n", "5000"],
        timeout=2.0,
    )
    if result is None or result.returncode != 0:
        return None
    if not session_key or not session_id:
        return None
    session_file_marker = f"/sessions/{session_id}.jsonl"
    latest = None
    for line in result.stdout.splitlines():
        if "[context-overflow-precheck]" not in line or session_file_marker not in line:
            continue
        match = _PROMPT_BUDGET_LOG_RE.search(line)
        if match and match.group("session_key") == session_key:
            latest = match
    if latest is None:
        return None
    estimated = int(latest.group("estimated"))
    prompt_budget = int(latest.group("prompt_budget"))
    overflow = int(latest.group("overflow"))
    reserve = int(latest.group("reserve"))
    resolved_context_budget = int(context_token_budget) if isinstance(context_token_budget, (int, float)) else prompt_budget + reserve
    return {
        "source": "gateway-journal",
        "estimatedPromptTokens": estimated,
        "contextTokenBudget": resolved_context_budget,
        "promptBudgetBeforeReserve": prompt_budget,
        "reserveTokens": reserve,
        "remainingPromptBudgetTokens": max(0, prompt_budget - estimated),
        "overflowTokens": overflow,
        "route": latest.group("route"),
    }


def _collect_erpmanager_context(timeout: float = 2.0) -> dict[str, Any]:
    return _collect_agent_context("erpmanager")


@app.get("/api/detail/erpmanager")
def detail_erpmanager() -> dict[str, Any]:
    try:
        return detail_response("erpmanager", _collect_erpmanager_context())
    except Exception as exc:
        return detail_response("erpmanager", {"error": _sanitize_error(exc)}, "error")


@app.get("/api/detail/secretary")
def detail_secretary() -> dict[str, Any]:
    try:
        return detail_response("secretary", _collect_agent_context("secretary"))
    except Exception as exc:
        return detail_response("secretary", {"error": _sanitize_error(exc)}, "error")
