from __future__ import annotations

import json
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class AuthorizationBackend(Protocol):
    """Backend contract; OpenClaw can replace LocalTestStore later."""

    def put_authorization(self, authorization_id: str, record: dict[str, Any]) -> None: ...
    def get_authorization(self, authorization_id: str) -> dict[str, Any] | None: ...
    def find_for_task(self, task_id: str) -> list[str]: ...
    def sign(self, record: dict[str, Any]) -> str: ...
    def verify(self, record: dict[str, Any], signature: str) -> bool: ...
    def is_used(self, authorization_id: str) -> bool: ...
    def mark_used(self, authorization_id: str) -> None: ...


class LocalTestStore:
    """JSON-backed local authorization store for tests and development."""

    def __init__(self, path: Path, signing_key: str | None = None):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if signing_key is not None and self.data.get("signing_key") != signing_key:
                raise ValueError("local authorization store signing key mismatch")
        else:
            self.data = {
                "schema_version": 2,
                "signing_key": signing_key or secrets.token_urlsafe(32),
                "authorizations": {},
                "used_authorization_ids": [],
            }
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def put_authorization(self, authorization_id: str, record: dict[str, Any]) -> None:
        self.data["authorizations"][authorization_id] = record
        self.save()

    def get_authorization(self, authorization_id: str) -> dict[str, Any] | None:
        raw = self.data.get("authorizations", {}).get(authorization_id)
        return dict(raw) if isinstance(raw, dict) else None

    def find_for_task(self, task_id: str) -> list[str]:
        return [
            str(auth_id)
            for auth_id, record in self.data.get("authorizations", {}).items()
            if isinstance(record, dict) and record.get("task_id") == task_id
        ]

    def sign(self, record: dict[str, Any]) -> str:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = str(self.data["signing_key"]).encode("utf-8")
        return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, record: dict[str, Any], signature: str) -> bool:
        return hmac.compare_digest(signature, self.sign(record))

    def is_used(self, authorization_id: str) -> bool:
        return authorization_id in self.data.setdefault("used_authorization_ids", [])

    def mark_used(self, authorization_id: str) -> None:
        used_ids = self.data.setdefault("used_authorization_ids", [])
        if authorization_id not in used_ids:
            used_ids.append(authorization_id)
            self.save()


class TaskAuthBroker:
    def __init__(self, store: AuthorizationBackend, audit_path: Path):
        self.store = store
        self.audit_path = audit_path

    def _signature(self, record: dict[str, Any]) -> str:
        return self.store.sign(record)

    def _audit(self, event: str, **fields: Any) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def issue(
        self,
        *,
        task_id: str,
        worker: str,
        allow: list[str],
        deny: list[str],
        expires_at: datetime,
        git_push_target: str | None = None,
        git_push_ref: str | None = None,
    ) -> str:
        authorization_id = secrets.token_hex(16)
        record = {
            "authorization_id": authorization_id,
            "task_id": task_id,
            "worker": worker,
            "allow": sorted(set(allow)),
            "deny": sorted(set(deny)),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "revoked": False,
            "git_push_target": git_push_target,
            "git_push_ref": git_push_ref,
        }
        record["signature"] = self._signature(record)
        self.store.put_authorization(authorization_id, record)
        self._audit(
            "issued",
            authorization_id=authorization_id,
            task_id=task_id,
            worker=worker,
            allow=record["allow"],
            deny=record["deny"],
        )
        return authorization_id

    def revoke(self, authorization_id: str) -> None:
        record = self.store.get_authorization(authorization_id)
        if record is None:
            raise ValueError("authorization not found")
        record.pop("signature", None)
        record["revoked"] = True
        record["signature"] = self._signature(record)
        self.store.put_authorization(authorization_id, record)
        self._audit(
            "revoked",
            authorization_id=authorization_id,
            task_id=record["task_id"],
            worker=record["worker"],
        )

    def authorize(
        self,
        *,
        authorization_id: str,
        task_id: str,
        worker: str,
        requested_actions: list[str],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._audit(
            "authorization_requested",
            authorization_id=authorization_id,
            task_id=task_id,
            worker=worker,
            requested_actions=requested_actions,
        )
        try:
            result = self._authorize(
                authorization_id=authorization_id,
                task_id=task_id,
                worker=worker,
                requested_actions=requested_actions,
                now=now,
            )
        except Exception as exc:
            self._audit(
                "authorization_denied",
                authorization_id=authorization_id,
                task_id=task_id,
                worker=worker,
                requested_actions=requested_actions,
                result="DENY",
                reason=str(exc),
            )
            raise
        self._audit(
            "authorization_used",
            authorization_id=authorization_id,
            task_id=task_id,
            worker=worker,
            requested_actions=requested_actions,
            result="ALLOW",
            reason="",
        )
        return result

    def _authorize(
        self,
        *,
        authorization_id: str,
        task_id: str,
        worker: str,
        requested_actions: list[str],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        raw = self.store.get_authorization(authorization_id)
        if raw is None:
            raise ValueError("authorization not found")
        record = dict(raw)
        supplied_signature = str(record.pop("signature", ""))
        if not self.store.verify(record, supplied_signature):
            raise ValueError("authorization signature mismatch")
        if record.get("revoked") is True:
            raise ValueError("authorization revoked")
        current_time = now or datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(str(record["expires_at"]))
        if current_time >= expires_at:
            raise ValueError("authorization expired")
        if record["task_id"] != task_id:
            raise ValueError("authorization task mismatch")
        if record["worker"] != worker:
            raise ValueError("authorization worker mismatch")
        allowed = set(record["allow"])
        denied = set(record["deny"])
        for action in requested_actions:
            if action in denied or action not in allowed:
                raise ValueError(f"action not authorized: {action}")
        if self.store.is_used(authorization_id):
            raise ValueError("authorization already used")
        self.store.mark_used(authorization_id)
        return {
            "broker_called": True,
            "authorization_id": authorization_id,
            "task_id": record["task_id"],
            "worker": record["worker"],
            "allow": list(record["allow"]),
            "deny": list(record["deny"]),
            "expires_at": record["expires_at"],
            "git_push_target": record.get("git_push_target"),
            "git_push_ref": record.get("git_push_ref"),
        }


def load_task_authorization(
    config_path: Path,
    task_id: str,
    worker: str | None = None,
    requested_actions: list[str] | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if data.get("schema_version") == 2:
        if not worker:
            raise ValueError("worker is required for signed authorization")
        authorizations = data.get("authorizations")
        if not isinstance(authorizations, dict):
            raise ValueError("signed authorization store is invalid")
        store = LocalTestStore(config_path)
        matches = store.find_for_task(task_id)
        if not matches:
            raise ValueError(f"authorization not found for task: {task_id}")
        if len(matches) != 1:
            raise ValueError(f"multiple authorizations found for task: {task_id}")
        broker = TaskAuthBroker(
            store,
            audit_path or config_path.with_name(config_path.stem + ".audit.jsonl"),
        )
        return broker.authorize(
            authorization_id=matches[0],
            task_id=task_id,
            worker=worker,
            requested_actions=list(requested_actions or []),
        )

    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("mock broker tasks must be an object")
    raw = tasks.get(task_id)
    if not isinstance(raw, dict):
        raise ValueError(f"authorization not found for task: {task_id}")

    allow = raw.get("allow", [])
    deny = raw.get("deny", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ValueError("authorization allow must be a string list")
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise ValueError("authorization deny must be a string list")

    return {
        "broker_called": True,
        "task_id": task_id,
        "allow": sorted(set(allow)),
        "deny": sorted(set(deny)),
        "git_push_target": raw.get("git_push_target"),
        "git_push_ref": raw.get("git_push_ref"),
    }
