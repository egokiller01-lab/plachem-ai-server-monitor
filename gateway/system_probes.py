from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from urllib.error import URLError
from urllib.request import urlopen


def http_reachable(
    url: str = "http://127.0.0.1:8080/v1/models",
    opener: Callable = urlopen,
) -> bool:
    try:
        with opener(url, timeout=3) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except OSError:
        return False


def comfy_has_work(
    url: str = "http://127.0.0.1:8188/api/queue",
    opener: Callable = urlopen,
) -> bool:
    try:
        with opener(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        return not isinstance(exc.reason, ConnectionRefusedError)
    except ConnectionRefusedError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    if not isinstance(payload, dict):
        return True
    if not isinstance(payload.get("queue_running"), list) or not isinstance(
        payload.get("queue_pending"), list
    ):
        return True
    return bool(payload["queue_running"] or payload["queue_pending"])


def parse_free_vram_mib(output: str) -> int:
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3 and "RTX 3090" in parts[0]:
            total = int(float(parts[1]))
            used = int(float(parts[2]))
            return max(0, total - used)
    return 0


def free_rtx3090_vram_mib(
    executor: Callable = subprocess.run,
    executable: str = "C:/Windows/System32/nvidia-smi.exe",
    attestor: Callable[[], None] | None = None,
) -> int:
    command = [
        executable,
        "--query-gpu=name,memory.total,memory.used",
        "--format=csv,noheader,nounits",
    ]
    try:
        if attestor is not None:
            attestor()
        completed = executor(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, ValueError):
        return 0
    if completed.returncode != 0:
        return 0
    return parse_free_vram_mib(completed.stdout)
