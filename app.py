from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
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


def get_gpu_from_nvidia_smi() -> dict[str, Any] | None:
    if not shutil.which("nvidia-smi"):
        return None
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    result = safe_run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    if not result or result.returncode != 0 or not result.stdout.strip():
        return None
    first = result.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 5:
        return None
    name, gpu_util, mem_used, mem_total, temp = parts[:5]
    used_gb = round(float(mem_used) / 1024, 1)
    total_gb = round(float(mem_total) / 1024, 1)
    vram_percent = round((float(mem_used) / float(mem_total)) * 100, 1) if float(mem_total) else None
    return {
        "name": name,
        "usage_percent": pct(gpu_util),
        "vram_used_gb": used_gb,
        "vram_total_gb": total_gb,
        "vram_usage_percent": vram_percent,
        "temperature_c": pct(temp),
        "status": "ok",
        "source": "nvidia-smi",
    }


def get_gpu_from_pynvml() -> dict[str, Any] | None:
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        pynvml.nvmlShutdown()
        return {
            "name": name,
            "usage_percent": pct(util.gpu),
            "vram_used_gb": bytes_to_gb(mem.used),
            "vram_total_gb": bytes_to_gb(mem.total),
            "vram_usage_percent": pct((mem.used / mem.total) * 100 if mem.total else None),
            "temperature_c": pct(temp),
            "status": "ok",
            "source": "pynvml",
        }
    except Exception:
        return None


def get_gpu() -> dict[str, Any]:
    try:
        return get_gpu_from_nvidia_smi() or get_gpu_from_pynvml() or {
            "name": "Unknown",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "status": "unknown",
            "source": "none",
        }
    except Exception as exc:
        return {
            "name": "Error",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "status": "error",
            "error": str(exc),
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


@app.get("/api/detail/services")
def detail_services() -> dict[str, Any]:
    try:
        return detail_response("services", {"services": extended_services()})
    except Exception as exc:
        return detail_response("services", {"error": str(exc)}, "error")


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
        "services": services,
    }
