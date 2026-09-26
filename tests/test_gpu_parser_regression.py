import subprocess
from unittest.mock import patch

import app


def _smi_result(row: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["nvidia-smi"], 0, row + "\n", "")


def test_cmp_170hx_nvidia_smi_na_optional_fields_keep_gpu_healthy():
    row = "NVIDIA CMP 170HX, [N/A], 1024, 65536, N/A, GPU-cmp, [N/A], N/A, [N/A], 00000000:04:00.0, N/A, N/A, N/A, N/A"
    metrics = app._gpu_metrics_from_nvidia_smi_row([part.strip() for part in row.split(",")], 0)

    assert metrics["status"] == "ok"
    assert metrics["name"] == "NVIDIA CMP 170HX"
    assert metrics["vram_used_gb"] == 1.0
    assert metrics["vram_total_gb"] == 64.0
    assert metrics["vram_usage_percent"] == 1.6
    assert metrics["usage_percent"] is None
    assert metrics["temperature_c"] is None
    assert metrics["power_draw_w"] is None
    assert metrics["fan_speed_percent"] is None
    assert metrics["pcie_gen_max"] is None


def test_nvidia_smi_ordinary_numeric_output_retains_metrics():
    row = "NVIDIA RTX 3090, 47, 22292, 24576, 65, GPU-ordinary, 220.5, 350, 30, 00000000:01:00.0, 4, 4, 16, 16"
    with patch.object(app.shutil, "which", return_value="/usr/bin/nvidia-smi"), patch.object(app, "safe_run", return_value=_smi_result(row)):
        gpus = app.get_gpus_from_nvidia_smi()

    assert len(gpus) == 1
    assert gpus[0]["status"] == "ok"
    assert gpus[0]["usage_percent"] == 47.0
    assert gpus[0]["temperature_c"] == 65.0
    assert gpus[0]["power_draw_w"] == 220.5
    assert gpus[0]["pcie_width_max"] == 16
