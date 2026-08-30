from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.path_security import reject_reparse_points, validate_pinned_file


def test_reject_reparse_points_detects_windows_file_attribute(monkeypatch, tmp_path: Path):
    target = tmp_path / "runtime" / "tasks"
    target.mkdir(parents=True)
    real_lstat = Path.lstat

    def fake_lstat(path: Path):
        if path == target:
            return SimpleNamespace(st_file_attributes=0x400)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(ValueError, match="reparse"):
        reject_reparse_points(target)


def test_validate_pinned_file_requires_exact_path_regular_file_and_hash(tmp_path: Path):
    import hashlib

    trusted = tmp_path / "trusted.exe"
    trusted.write_bytes(b"trusted")
    digest = hashlib.sha256(b"trusted").hexdigest()

    assert validate_pinned_file(trusted, trusted, digest) == trusted.resolve()
    with pytest.raises(ValueError, match="pinned path"):
        validate_pinned_file(tmp_path / "." / "other.exe", trusted, digest)
    with pytest.raises(ValueError, match="SHA-256"):
        validate_pinned_file(trusted, trusted, "0" * 64)
    with pytest.raises(ValueError, match="regular file"):
        validate_pinned_file(tmp_path, tmp_path, hashlib.sha256(b"").hexdigest())
