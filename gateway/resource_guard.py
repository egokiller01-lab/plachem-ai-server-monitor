from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from gateway.path_security import reject_reparse_points


class ResourceBusy(RuntimeError):
    pass


def canonical_gpu_lock_path() -> Path:
    return Path("C:/ProgramData/PLACHEM-Agent-Control/rtx3090.lock")


class ResourceGuard:
    def __init__(
        self,
        lock_path: Path,
        llm_reachable: Callable[[], bool],
        comfy_running: Callable[[], bool],
        free_vram_mib: Callable[[], int],
        minimum_free_vram_mib: int = 512,
    ) -> None:
        self.lock_path = lock_path
        self.llm_reachable = llm_reachable
        self.comfy_running = comfy_running
        self.free_vram_mib = free_vram_mib
        self.minimum_free_vram_mib = minimum_free_vram_mib
        self._owned = False
        self._handle = None
        self._owned_identity: tuple[int, int] | None = None

    def acquire(self) -> None:
        reject_reparse_points(self.lock_path.parent)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_points(self.lock_path.parent)
        reject_reparse_points(self.lock_path)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ResourceBusy("RTX 3090 is already locked") from exc
        handle = os.fdopen(fd, "w+", encoding="utf-8")
        created = os.fstat(handle.fileno())
        created_identity = (created.st_dev, created.st_ino)
        try:
            handle.write(str(os.getpid()))
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            handle.close()
            try:
                current = self.lock_path.stat(follow_symlinks=False)
                if created_identity == (current.st_dev, current.st_ino):
                    self.lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        metadata = os.fstat(handle.fileno())
        self._handle = handle
        self._owned_identity = (metadata.st_dev, metadata.st_ino)
        self._owned = True
        try:
            if not self.llm_reachable():
                raise ResourceBusy("Achilles LLM endpoint is not reachable")
            if self.comfy_running():
                raise ResourceBusy("ComfyUI has running or pending work")
            free_mib = self.free_vram_mib()
            if free_mib < self.minimum_free_vram_mib:
                raise ResourceBusy(f"insufficient RTX 3090 VRAM: {free_mib} MiB free")
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        if not self._owned or self._handle is None:
            return
        handle = self._handle
        identity = self._owned_identity
        same_path = False
        try:
            metadata = self.lock_path.stat(follow_symlinks=False)
            same_path = identity == (metadata.st_dev, metadata.st_ino)
        except OSError:
            pass
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            self._owned = False
            self._owned_identity = None
        if same_path:
            try:
                metadata = self.lock_path.stat(follow_symlinks=False)
                if identity == (metadata.st_dev, metadata.st_ino):
                    self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "ResourceGuard":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
