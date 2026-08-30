import io
import subprocess
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from gateway.system_probes import comfy_has_work, free_rtx3090_vram_mib, parse_free_vram_mib


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_comfy_probe_detects_running_queue():
    def opener(url, timeout):
        return Response(b'{"queue_running":[[1]],"queue_pending":[]}')

    assert comfy_has_work(opener=opener) is True


def test_parse_free_vram_uses_rtx_3090_row():
    output = "NVIDIA GeForce RTX 3090, 24576, 2048\nNVIDIA GeForce RTX 3070, 8192, 700"
    assert parse_free_vram_mib(output) == 22528


def test_free_vram_probe_uses_pinned_absolute_executable():
    def execute(command, **kwargs):
        assert command[0] == "C:/Windows/System32/nvidia-smi.exe"
        return subprocess.CompletedProcess(command, 0, "NVIDIA GeForce RTX 3090, 24576, 2048", "")

    assert free_rtx3090_vram_mib(executor=execute) == 22528


def test_free_vram_attests_binary_before_subprocess():
    calls = []

    def attest():
        calls.append("attest")

    def executor(*args, **kwargs):
        calls.append("execute")
        return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 3090, 24576, 1000")

    assert free_rtx3090_vram_mib(executor=executor, attestor=attest) == 23576
    assert calls == ["attest", "execute"]


def test_comfy_probe_fails_closed_on_malformed_response():
    assert comfy_has_work(opener=lambda url, timeout: Response(b"not-json")) is True


def test_comfy_probe_treats_only_explicit_connection_refusal_as_idle():
    def refused(url, timeout):
        raise URLError(ConnectionRefusedError(10061, "refused"))

    assert comfy_has_work(opener=refused) is False
