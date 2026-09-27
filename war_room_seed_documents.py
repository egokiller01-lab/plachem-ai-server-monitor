from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import war_room
from war_room_actions import provision_action_schema
from war_room_documents import DocumentRegistrationError, register_document


CATEGORY_BY_NAME = {
    "00-CANONICAL-STATE.md": "reference",
    "01-ARCHITECTURE.md": "architecture",
    "02-PROJECT-CONTROL.md": "requirements",
    "03-VERIFIED-BASELINES.md": "reference",
    "04-DECISIONS.md": "decision",
    "05-RND-BACKLOG.md": "requirements",
    "06-DOCUMENT-REGISTRY.md": "architecture",
    "90-HISTORY.md": "report",
}


def seed(
    *,
    db_path: Path,
    worktree: Path,
    project_id: str = war_room.PROJECT_ID,
) -> dict[str, object]:
    worktree = worktree.expanduser().resolve()
    document_dir = worktree / "docs" / "openclaw-project"
    if not document_dir.is_dir():
        raise SystemExit(f"document directory not found: {document_dir}")
    if not db_path.is_file():
        raise SystemExit(f"War Room database not found: {db_path}")

    provision_action_schema(str(db_path))
    results: list[dict[str, object]] = []
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        project = con.execute("SELECT id FROM war_projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise SystemExit(f"project not found: {project_id}")
        for path in sorted(document_dir.glob("*.md")):
            category = CATEGORY_BY_NAME.get(path.name, "reference")
            try:
                result = register_document(
                    con,
                    project_id=project_id,
                    title=path.stem.replace("-", " ").replace("_", " "),
                    uri=str(path),
                    approved_roots=[str(worktree)],
                    created_by="migration:project-doc-registry",
                    category=category,
                    summary=f"Initial project document registry import: {path.name}",
                    relation="reference",
                )
            except DocumentRegistrationError as exc:
                results.append({"file": path.name, "status": "rejected", "reason": str(exc)})
                continue
            status = "registered" if result["changed"] else "unchanged"
            results.append({
                "file": path.name,
                "status": status,
                "document_id": result["document_id"],
                "version": result["version"],
                "sha256": result["sha256"],
            })
            if result["changed"]:
                con.execute(
                    "INSERT INTO war_audit_events VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), project_id, "migration:project-doc-registry",
                        "document_seed_registered", "document", result["document_id"],
                        json.dumps({
                            "file": path.name,
                            "version": result["version"],
                            "uri": result["uri"],
                            "sha256": result["sha256"],
                        }, sort_keys=True),
                        str(uuid.uuid4()), int(time.time()),
                    ),
                )
        con.commit()
    return {
        "project_id": project_id,
        "db_path": str(db_path),
        "worktree": str(worktree),
        "registered": sum(1 for item in results if item["status"] == "registered"),
        "unchanged": sum(1 for item in results if item["status"] == "unchanged"),
        "rejected": sum(1 for item in results if item["status"] == "rejected"),
        "items": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed canonical War Room project documents into the Registry.")
    parser.add_argument("--db", default=str(war_room._db_path()), help="War Room SQLite database path")
    parser.add_argument(
        "--worktree",
        default=os.environ.get("PLACHEM_WAR_ROOM_WORKTREE") or str(Path.cwd()),
        help="Repository/worktree containing docs/openclaw-project",
    )
    parser.add_argument("--project-id", default=war_room.PROJECT_ID)
    args = parser.parse_args()
    result = seed(db_path=Path(args.db).expanduser(), worktree=Path(args.worktree), project_id=args.project_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
