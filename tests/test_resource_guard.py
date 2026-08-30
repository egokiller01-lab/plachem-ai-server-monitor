from pathlib import Path

import pytest

from gateway.resource_guard import (
    ResourceBusy,
    ResourceGuard,
    canonical_gpu_lock_path,
)


def test_blocks_when_comfyui_has_running_work(tmp_path: Path):
    guard = ResourceGuard(
        lock_path=tmp_path / "gpu3090.lock",
        llm_reachable=lambda: True,
        comfy_running=lambda: True,
        free_vram_mib=lambda: 20000,
    )

    with pytest.raises(ResourceBusy, match="ComfyUI"):
        guard.acquire()


def test_acquire_creates_single_gpu_lock_and_release_removes_it(tmp_path: Path):
    lock = tmp_path / "gpu3090.lock"
    guard = ResourceGuard(
        lock_path=lock,
        llm_reachable=lambda: True,
        comfy_running=lambda: False,
        free_vram_mib=lambda: 20000,
    )

    guard.acquire()
    assert lock.exists()
    with pytest.raises(ResourceBusy, match="already locked"):
        guard.acquire()
    guard.release()
    assert not lock.exists()


def test_default_threshold_accepts_loaded_achilles_server_headroom(tmp_path: Path):
    guard = ResourceGuard(
        lock_path=tmp_path / "gpu3090.lock",
        llm_reachable=lambda: True,
        comfy_running=lambda: False,
        free_vram_mib=lambda: 1_236,
    )
    guard.acquire()
    guard.release()


def test_guard_locks_before_probes_and_releases_after_failed_probe(tmp_path: Path):
    lock = tmp_path / "gpu3090.lock"

    def unreachable():
        assert lock.exists()
        return False

    guard = ResourceGuard(
        lock_path=lock,
        llm_reachable=unreachable,
        comfy_running=lambda: False,
        free_vram_mib=lambda: 20_000,
    )

    with pytest.raises(ResourceBusy, match="not reachable"):
        guard.acquire()
    assert not lock.exists()


def test_canonical_gpu_lock_is_fixed_machine_wide_path(monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", "D:/attacker-controlled")
    monkeypatch.setenv("SYSTEMDRIVE", "Z:")
    assert canonical_gpu_lock_path() == Path("C:/ProgramData/PLACHEM-Agent-Control/rtx3090.lock")


def test_guard_holds_lock_descriptor_for_entire_lifetime(tmp_path: Path):
    lock = tmp_path / "gpu3090.lock"
    guard = ResourceGuard(lock, lambda: True, lambda: False, lambda: 20_000)
    guard.acquire()
    assert guard._handle is not None
    assert guard._handle.closed is False
    guard.release()
    assert guard._handle is None


def test_release_does_not_unlink_a_path_whose_identity_was_replaced(tmp_path: Path):
    lock = tmp_path / "gpu3090.lock"
    guard = ResourceGuard(lock, lambda: True, lambda: False, lambda: 20_000)
    guard.acquire()
    guard._owned_identity = (-1, -1)
    guard.release()
    assert lock.exists()
