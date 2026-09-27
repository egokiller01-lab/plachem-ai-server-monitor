# War Room Simple Mode — QA Result

Date: 2026-09-27

## Goal

Add a representative-facing Simple Mode without removing or rewriting the existing Advanced War Room.

## Simple Mode surface

- Project selector
- Project status summary
- New task instruction
- Assignee / QA reviewer only
- Current tasks
- Representative approve + run
- Task stop
- Reapproval preparation
- Recent worker / QA results
- Representative final approval
- Project documents
- One-click Advanced Mode link
- Current-task view limited to tasks updated within 72 hours or backed by a live delivery
- Older unfinished lifecycle records are summarized as hidden historical cleanup items instead of presented as current work

Hidden from the Simple UI:

- call / turn limits
- delivery IDs
- session IDs
- correlation IDs
- JEV probability/details
- audit controls
- manual Evidence registration
- participant administration
- ManyFast technical controls

## Safety

- Existing backend APIs are reused.
- task_approve_execute, task_stop and representative_completion keep the existing fresh mutation-context protection.
- No new execution engine or lifecycle state was introduced.
- Existing /war-room remains Advanced during representative review.
- New /war-room/simple is isolated.
- /war-room/advanced explicitly preserves the existing UI.

## QA

Focused Simple/Auth tests:
- 3 passed

Full War Room regression:
- 220 passed
- 7 warnings
- 5 subtests passed

Warnings are existing FastAPI/TestClient deprecation warnings.

Static checks:
- python3 -m py_compile app.py: PASS
- node --check static/war-room-simple.js: PASS
- git diff --check: PASS
