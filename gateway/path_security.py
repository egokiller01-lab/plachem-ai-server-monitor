from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def reject_reparse_points(path: Path) -> None:
    """Fail closed if any existing path component is a link/junction/reparse point."""
    absolute = path.absolute()
    components = [absolute]
    components.extend(absolute.parents)
    for component in reversed(components):
        if not os.path.lexists(component):
            continue
        metadata = component.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        mode = getattr(metadata, "st_mode", 0)
        if stat.S_ISLNK(mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"runtime path contains a reparse point: {component}")


def validate_pinned_file(path: Path, pinned_path: Path, expected_sha256: str) -> Path:
    """Validate a policy-pinned, regular, non-reparse file before use."""
    if not path.is_absolute() or not pinned_path.is_absolute():
        raise ValueError("trusted file must use an absolute pinned path")
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(pinned_path)):
        raise ValueError("trusted file does not match its pinned path")
    reject_reparse_points(path)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("trusted file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("trusted path is not a regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("trusted file could not be read") from exc
    if digest.hexdigest() != expected_sha256:
        raise ValueError("trusted file SHA-256 mismatch")
    return path.resolve(strict=True)
