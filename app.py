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


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PLACHEM AI Server Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
        return {"usage_percent": pct(psutil.cpu_percent(interval=None)), "status": "ok"}
    except Exception as exc:
        return {"usage_percent": None, "status": "error", "error": str(exc)}


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
    try:
        used = float(mem_used)
        total = float(mem_total)
    except Exception:
        return None
    used_gb = round(used / 1024, 1)
    total_gb = round(total / 1024, 1)
    vram_percent = round((used / total) * 100, 1) if total else None
    return {
        "index": index,
        "name": name,
        "usage_percent": pct(gpu_util),
        "vram_used_gb": used_gb,
        "vram_total_gb": total_gb,
        "vram_usage_percent": vram_percent,
        "temperature_c": pct(temp),
        "status": "ok",
        "source": "nvidia-smi",
    }


def get_gpus_from_nvidia_smi() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    result = safe_run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(result.stdout.strip().splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            out.append({
                "index": i,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "status": "error",
                "source": "nvidia-smi",
                "error": "invalid nvidia-smi row",
            })
            continue
        metrics = _gpu_metrics_from_nvidia_smi_row(parts, i)
        if metrics is None:
            out.append({
                "index": i,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
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
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            out.append({
                "index": i,
                "name": name,
                "usage_percent": pct(util.gpu),
                "vram_used_gb": bytes_to_gb(mem.used),
                "vram_total_gb": bytes_to_gb(mem.total),
                "vram_usage_percent": pct((mem.used / mem.total) * 100 if mem.total else None),
                "temperature_c": pct(temp),
                "status": "ok",
                "source": "pynvml",
            })
        except Exception as exc:
            out.append({
                "index": i,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "status": "error",
                "source": "pynvml",
                "error": str(exc),
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
            "name": "Unknown",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "status": "unknown",
            "source": "none",
        }]
    except Exception as exc:
        return [{
            "index": 0,
            "name": "Error",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "status": "error",
            "source": "none",
            "error": str(exc),
        }]


def get_gpu() -> dict[str, Any]:
    gpus = get_gpus()
    return gpus[0] if gpus else {
        "name": "Unknown",
        "usage_percent": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_usage_percent": None,
        "temperature_c": None,
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
    query = "pid,process_name,used_memory"
    result = safe_run(["nvidia-smi", f"--query-compute-apps={query}", "--format=csv,noheader,nounits"], timeout=1.5)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            rows.append({"pid": int(parts[0]), "name": parts[1], "vram_mb": float(parts[2])})
        except Exception:
            continue
    return sorted(rows, key=lambda item: item["vram_mb"], reverse=True)


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    cpu = get_cpu()
    memory = get_memory()
    disk = get_disk()
    network = get_network()
    gpu = get_gpu()
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
        "gpus": get_gpus(),
        "services": services,
    }
