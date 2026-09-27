from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DOCUMENT_CATEGORIES = frozenset({
    "requirements", "architecture", "decision", "reference", "report", "handoff", "other",
})
DOCUMENT_RELATIONS = frozenset({"input", "output", "reference", "decision", "handoff"})
DOCUMENT_EXTENSIONS = frozenset({".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".json"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS war_documents (
 id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL REFERENCES war_projects(id),
 title TEXT NOT NULL,
 category TEXT NOT NULL CHECK(category IN ('requirements','architecture','decision','reference','report','handoff','other')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','archived')),
 current_version_id TEXT,
 created_by TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_document_versions (
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES war_documents(id),
 version INTEGER NOT NULL CHECK(version >= 1),
 uri TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
 mime_type TEXT,
 source_task_id TEXT REFERENCES war_tasks(id),
 source_agent_id TEXT,
 source_session_id TEXT,
 source_run_id TEXT,
 summary TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL,
 UNIQUE(document_id, version)
);
CREATE TABLE IF NOT EXISTS war_document_links (
 document_id TEXT NOT NULL REFERENCES war_documents(id),
 task_id TEXT NOT NULL REFERENCES war_tasks(id),
 relation TEXT NOT NULL CHECK(relation IN ('input','output','reference','decision','handoff')),
 created_at INTEGER NOT NULL,
 PRIMARY KEY(document_id, task_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_war_documents_project_category
 ON war_documents(project_id, category, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_war_document_versions_document
 ON war_document_versions(document_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_war_document_versions_task
 ON war_document_versions(source_task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_war_document_links_task
 ON war_document_links(task_id, relation);
CREATE TRIGGER IF NOT EXISTS war_document_versions_no_update
 BEFORE UPDATE ON war_document_versions BEGIN SELECT RAISE(ABORT, 'document versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_document_versions_no_delete
 BEFORE DELETE ON war_document_versions BEGIN SELECT RAISE(ABORT, 'document versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_document_links_no_update
 BEFORE UPDATE ON war_document_links BEGIN SELECT RAISE(ABORT, 'document links are append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_document_links_no_delete
 BEFORE DELETE ON war_document_links BEGIN SELECT RAISE(ABORT, 'document links are append-only'); END;
"""


class DocumentRegistrationError(ValueError):
    pass


def provision_document_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _now() -> int:
    return int(time.time())


def _safe_path(path_value: str, roots: list[str]) -> Path:
    if not isinstance(path_value, str) or not path_value.startswith("/"):
        raise DocumentRegistrationError("document path must be absolute")
    candidate = Path(path_value).expanduser()
    if ".." in candidate.parts:
        raise DocumentRegistrationError("document path traversal is not allowed")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DocumentRegistrationError("document path does not exist") from exc
    if not resolved.is_file():
        raise DocumentRegistrationError("document path must be a file")
    for raw_root in roots:
        if not isinstance(raw_root, str) or not raw_root.startswith("/"):
            continue
        root = Path(raw_root).expanduser()
        if ".." in root.parts:
            continue
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            resolved_root = root.resolve(strict=False)
        if resolved == resolved_root or resolved_root in resolved.parents:
            return resolved
    raise DocumentRegistrationError("document path is outside approved roots")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _infer_category(path: Path) -> str:
    name = path.name.lower()
    if "require" in name or "prd" in name:
        return "requirements"
    if "architect" in name or "design" in name:
        return "architecture"
    if "decision" in name or "adr" in name:
        return "decision"
    if "handoff" in name or "recovery" in name:
        return "handoff"
    if "report" in name or "result" in name or "summary" in name:
        return "report"
    if "reference" in name or "baseline" in name:
        return "reference"
    return "other"


def _validate_category(value: str | None, path: Path) -> str:
    category = str(value or "").strip().lower() or _infer_category(path)
    if category not in DOCUMENT_CATEGORIES:
        raise DocumentRegistrationError("invalid document category")
    return category


def _validate_relation(value: str | None, category: str) -> str:
    relation = str(value or "").strip().lower()
    if not relation:
        relation = "decision" if category == "decision" else "handoff" if category == "handoff" else "output"
    if relation not in DOCUMENT_RELATIONS:
        raise DocumentRegistrationError("invalid document relation")
    return relation


def _current_version(con: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return con.execute(
        """SELECT v.* FROM war_documents d
           LEFT JOIN war_document_versions v ON v.id=d.current_version_id
           WHERE d.id=?""",
        (document_id,),
    ).fetchone()


def register_document(
    con: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    uri: str,
    approved_roots: list[str],
    created_by: str,
    category: str | None = None,
    summary: str = "",
    source_task_id: str | None = None,
    source_agent_id: str | None = None,
    source_session_id: str | None = None,
    source_run_id: str | None = None,
    document_id: str | None = None,
    expected_version: int | None = None,
    relation: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    project = con.execute("SELECT id,status FROM war_projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise DocumentRegistrationError("project not found")
    if str(project["status"]) == "archived":
        raise DocumentRegistrationError("project is archived")
    clean_title = str(title or "").strip()
    if not clean_title or len(clean_title) > 300:
        raise DocumentRegistrationError("document title must be 1..300 characters")
    path = _safe_path(uri, approved_roots)
    if path.suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise DocumentRegistrationError("unsupported document file type")
    clean_category = _validate_category(category, path)
    clean_relation = _validate_relation(relation, clean_category)
    created_at = _now() if now is None else int(now)
    digest = _sha256(path)
    size = path.stat().st_size
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    existing = None
    if document_id:
        existing = con.execute(
            "SELECT * FROM war_documents WHERE id=? AND project_id=?",
            (document_id, project_id),
        ).fetchone()
        if not existing:
            raise DocumentRegistrationError("document not found in project")
    else:
        existing = con.execute(
            """SELECT d.* FROM war_documents d
               JOIN war_document_versions v ON v.id=d.current_version_id
               WHERE d.project_id=? AND v.uri=? AND d.status='active'
               ORDER BY d.updated_at DESC LIMIT 1""",
            (project_id, str(path)),
        ).fetchone()

    if existing:
        document_id = str(existing["id"])
        current = _current_version(con, document_id)
        current_number = int(current["version"] or 0) if current else 0
        if expected_version is not None and int(expected_version) != current_number:
            raise DocumentRegistrationError(
                f"version conflict: expected {expected_version}, current {current_number}"
            )
        if current and current["sha256"] == digest and current["uri"] == str(path):
            version_id = str(current["id"])
            version_number = current_number
            changed = False
        else:
            version_number = current_number + 1
            version_id = str(uuid.uuid4())
            con.execute(
                """INSERT INTO war_document_versions
                   (id,document_id,version,uri,sha256,size_bytes,mime_type,source_task_id,
                    source_agent_id,source_session_id,source_run_id,summary,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, document_id, version_number, str(path), digest, size, mime_type,
                    source_task_id, source_agent_id, source_session_id, source_run_id,
                    str(summary or "")[:4096], created_at,
                ),
            )
            con.execute(
                """UPDATE war_documents SET title=?,category=?,current_version_id=?,updated_at=?
                   WHERE id=?""",
                (clean_title, clean_category, version_id, created_at, document_id),
            )
            changed = True
    else:
        if expected_version not in (None, 0):
            raise DocumentRegistrationError("version conflict: document does not exist")
        document_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        version_number = 1
        con.execute(
            """INSERT INTO war_documents
               (id,project_id,title,category,status,current_version_id,created_by,created_at,updated_at)
               VALUES (?,?,?,?, 'active', ?,?,?,?)""",
            (
                document_id, project_id, clean_title, clean_category, version_id,
                created_by, created_at, created_at,
            ),
        )
        con.execute(
            """INSERT INTO war_document_versions
               (id,document_id,version,uri,sha256,size_bytes,mime_type,source_task_id,
                source_agent_id,source_session_id,source_run_id,summary,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, document_id, 1, str(path), digest, size, mime_type,
                source_task_id, source_agent_id, source_session_id, source_run_id,
                str(summary or "")[:4096], created_at,
            ),
        )
        changed = True

    if source_task_id:
        task = con.execute("SELECT project_id FROM war_tasks WHERE id=?", (source_task_id,)).fetchone()
        if not task or str(task["project_id"]) != project_id:
            raise DocumentRegistrationError("task does not belong to project")
        con.execute(
            """INSERT OR IGNORE INTO war_document_links(document_id,task_id,relation,created_at)
               VALUES (?,?,?,?)""",
            (document_id, source_task_id, clean_relation, created_at),
        )

    return {
        "document_id": document_id,
        "version_id": version_id,
        "version": version_number,
        "uri": str(path),
        "sha256": digest,
        "category": clean_category,
        "relation": clean_relation,
        "changed": changed,
    }


def _row_to_document(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "category": row["category"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "current_version": row["version"],
        "version_id": row["version_id"],
        "uri": row["uri"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "mime_type": row["mime_type"],
        "summary": row["summary"],
        "source_task_id": row["source_task_id"],
        "source_agent_id": row["source_agent_id"],
        "source_session_id": row["source_session_id"],
        "source_run_id": row["source_run_id"],
    }


def list_project_documents(
    con: sqlite3.Connection,
    project_id: str,
    *,
    task_id: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [project_id]
    where = ["d.project_id=?"]
    join = ""
    if task_id:
        join = " JOIN war_document_links l ON l.document_id=d.id "
        where.append("l.task_id=?")
        params.append(task_id)
    if category:
        if category not in DOCUMENT_CATEGORIES:
            raise DocumentRegistrationError("invalid document category")
        where.append("d.category=?")
        params.append(category)
    rows = con.execute(
        f"""SELECT d.*,v.id AS version_id,v.version,v.uri,v.sha256,v.size_bytes,v.mime_type,
                   v.summary,v.source_task_id,v.source_agent_id,v.source_session_id,v.source_run_id
            FROM war_documents d
            JOIN war_document_versions v ON v.id=d.current_version_id
            {join}
            WHERE {' AND '.join(where)}
            ORDER BY d.category,d.title,d.updated_at DESC""",
        params,
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def get_document(con: sqlite3.Connection, document_id: str) -> dict[str, Any] | None:
    row = con.execute(
        """SELECT d.*,v.id AS version_id,v.version,v.uri,v.sha256,v.size_bytes,v.mime_type,
                  v.summary,v.source_task_id,v.source_agent_id,v.source_session_id,v.source_run_id
           FROM war_documents d
           JOIN war_document_versions v ON v.id=d.current_version_id
           WHERE d.id=?""",
        (document_id,),
    ).fetchone()
    if not row:
        return None
    result = _row_to_document(row)
    result["links"] = [
        dict(item) for item in con.execute(
            "SELECT task_id,relation,created_at FROM war_document_links WHERE document_id=? ORDER BY created_at",
            (document_id,),
        ).fetchall()
    ]
    return result


def list_versions(con: sqlite3.Connection, document_id: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in con.execute(
            """SELECT id,document_id,version,uri,sha256,size_bytes,mime_type,source_task_id,
                      source_agent_id,source_session_id,source_run_id,summary,created_at
               FROM war_document_versions WHERE document_id=? ORDER BY version DESC""",
            (document_id,),
        ).fetchall()
    ]


def context_documents(
    con: sqlite3.Connection,
    project_id: str,
    *,
    required_ids: list[str] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    documents = list_project_documents(con, project_id)
    if required_ids:
        index = {item["id"]: item for item in documents}
        return [
            {
                "document_id": index[doc_id]["id"],
                "title": index[doc_id]["title"],
                "category": index[doc_id]["category"],
                "version": index[doc_id]["current_version"],
                "uri": index[doc_id]["uri"],
                "sha256": index[doc_id]["sha256"],
                "summary": index[doc_id]["summary"],
            }
            for doc_id in required_ids if doc_id in index
        ][:limit]
    preferred = {"requirements": 0, "architecture": 1, "decision": 2, "handoff": 3, "reference": 4, "report": 5, "other": 6}
    documents.sort(key=lambda item: (preferred.get(item["category"], 9), -int(item["updated_at"] or 0)))
    return [
        {
            "document_id": item["id"],
            "title": item["title"],
            "category": item["category"],
            "version": item["current_version"],
            "uri": item["uri"],
            "sha256": item["sha256"],
            "summary": item["summary"],
        }
        for item in documents[:limit]
    ]


def document_candidate(path_value: str) -> bool:
    try:
        return Path(path_value).suffix.lower() in DOCUMENT_EXTENSIONS
    except (TypeError, ValueError):
        return False


def inferred_title(path_value: str) -> str:
    path = Path(path_value)
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name
