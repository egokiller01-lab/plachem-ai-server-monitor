from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from gateway.models import TaskSpec
from gateway.workspace import StagedWorkspace


class LocalValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidationReport:
    checks: list[str]


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append(values["src"] or "")
        if tag == "link" and values.get("href"):
            self.references.append(values["href"] or "")


_EXTERNAL_PATTERN = re.compile(
    r"https?://|(?<![A-Za-z])//[A-Za-z]|\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\s*\(",
    re.IGNORECASE,
)


class LocalValidationAdapter:
    def validate(self, task: TaskSpec, staged: StagedWorkspace) -> ValidationReport:
        artifact_paths = set(staged.hashes)
        texts: dict[str, str] = {}
        for relative in sorted(artifact_paths):
            path = staged.root / relative
            try:
                data = path.read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise LocalValidationError(f"artifact is not readable UTF-8: {relative}") from exc
            if not text.strip():
                raise LocalValidationError(f"artifact is empty: {relative}")
            texts[relative] = text

        if "external_network" in task.deny:
            for relative, text in texts.items():
                if _EXTERNAL_PATTERN.search(text):
                    raise LocalValidationError(f"external network use is denied: {relative}")

        for relative, text in texts.items():
            if not relative.lower().endswith(('.html', '.htm')):
                continue
            parser = _AssetParser()
            try:
                parser.feed(text)
                parser.close()
            except Exception as exc:
                raise LocalValidationError(f"HTML parsing failed: {relative}") from exc
            base = PurePosixPath(relative).parent
            for reference in parser.references:
                parsed = urlparse(reference)
                if parsed.scheme or parsed.netloc or reference.startswith("//"):
                    raise LocalValidationError(f"external network reference is denied: {reference}")
                if reference.startswith(("#", "data:")):
                    continue
                normalized = (base / parsed.path).as_posix()
                if any(part == ".." for part in PurePosixPath(normalized).parts):
                    raise LocalValidationError(f"asset reference escapes scope: {reference}")
                if normalized not in artifact_paths:
                    raise LocalValidationError(f"missing local asset: {normalized}")

        combined = "\n".join(texts[path] for path in sorted(texts))
        for required in task.validation.required_text:
            if required not in combined:
                raise LocalValidationError(f"required text is missing: {required}")
        for pattern in task.validation.required_regex:
            if re.search(pattern, combined) is None:
                raise LocalValidationError(f"required regex is missing: {pattern}")
        if task.validation.javascript_syntax:
            javascript = sorted(path for path in artifact_paths if path.lower().endswith(".js"))
            if not javascript:
                raise LocalValidationError("JavaScript syntax validation requested without a JavaScript artifact")
            for relative in javascript:
                try:
                    completed = subprocess.run(
                        ["node", "--check", str(staged.root / relative)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=30, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise LocalValidationError("JavaScript syntax validator could not run") from exc
                if completed.returncode != 0:
                    raise LocalValidationError(f"JavaScript syntax failed: {relative}")

        checks = [
            f"{len(texts)} non-empty UTF-8 artifacts",
            "static web references are scoped and local",
            "external network policy validated",
        ]
        if task.validation.required_text:
            checks.append("TaskSpec required text validated")
        if task.validation.required_regex:
            checks.append("TaskSpec required regex validated")
        if task.validation.javascript_syntax:
            checks.append("JavaScript syntax validated")
        return ValidationReport(checks=checks)
