# War Room Simple Mode — All Available Agents Fix

Date: 2026-09-28

## Problem

Simple Mode filtered the assignee/reviewer lists by the current project's participant set. This made available server agents disappear from the UI when they had not yet been added to that project.

## Fix

- Assignee list now uses the full registered, enabled, execution-eligible Agent catalog.
- QA reviewer list now uses the full enabled QA-capable Agent catalog.
- Existing project participation is shown normally.
- Non-participating Agents are labeled as joining the project on selection.
- Inactive participants are reactivated when selected.
- A newly selected Worker is added as developer before Task prepare.
- A newly selected QA reviewer is added as QA before Task prepare.
- Existing active participant roles are preserved unless a selected QA Agent must be assigned the QA role.
- Worker and reviewer must remain different.

## Functional QA

Isolated test:
- Added dynamic FlexDev to the Agent catalog.
- Confirmed FlexDev execution_eligible=true and participating=false.
- Added FlexDev as a project participant.
- Prepared a FAST_GATEWAY Task with FlexDev as assignee and ERPqa as reviewer.
- Result: PASS.

## Regression

Command:

    python3 -m pytest -q tests/test_war_room*.py

Result:
- 221 passed
- 7 warnings
- 5 subtests passed

Warnings are existing FastAPI/TestClient deprecation warnings.

## Static checks

- node --check static/war-room-simple.js: PASS
- git diff --check: PASS
