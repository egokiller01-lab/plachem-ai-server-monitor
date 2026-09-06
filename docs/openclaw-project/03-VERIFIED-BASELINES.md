# Verified Baselines

All requested labels are listed individually. Each entry names Date, Result, Files, and Evidence. `VERIFIED` is used only where the 2026-09-04/05 record explicitly reported it; otherwise the result is `UNVERIFIED`.

1. **War Room↔Fast Gateway Phase1**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported Phase 1 scope.
   - Files: `war_room_fast_gateway.py`, `war_room_actions.py`, `war_room_runtime.py`, `war_room_worker.py`.
   - Evidence: Phase 1 bridge/stop evidence and reported regression/E2E PASS.
2. **Persistent Harness**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported bounded ownership and terminal-handling scope.
   - Files: `fast_gateway_service.py`, `plachem_fast_gateway/core_engine.py`.
   - Evidence: historical harness record and terminal reconciliation evidence.
3. **persistent owner connection**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported run-binding scope.
   - Files: `war_room_fast_gateway.py`, `plachem_fast_gateway/openclaw_adapter.py`.
   - Evidence: owning OpenClaw session binding was resolved for cancellation.
4. **Background terminal reconciliation**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported historical run.
   - Files: `fast_gateway_service.py`, `plachem_fast_gateway/core_engine.py`.
   - Evidence: worker termination reconciled to one terminal record without external wait.
5. **sessions.abort user cancel**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported LOCAL stop path.
   - Files: `war_room_actions.py`, `war_room_fast_gateway.py`, `plachem_fast_gateway/openclaw_adapter.py`.
   - Evidence: `sessions.abort` yielded `CANCELLED / USER_CANCEL`; reported stop E2E PASS.
6. **chat.abort Runtime Policy cancel**
   - Date: 2026-09-05.
   - Result: `UNVERIFIED`; no exact current acceptance evidence is recorded here.
   - Files: Runtime Policy and chat cancellation implementation references.
   - Evidence: no qualifying command/result or UI evidence available in this docs-only task.
7. **LOCAL Result Format Recovery**
   - Date: 2026-09-05.
   - Result: `UNVERIFIED`; no exact current acceptance evidence is recorded here.
   - Files: LOCAL result-format/recovery implementation references.
   - Evidence: no qualifying command/result evidence available in this docs-only task.
8. **caller timeout override blocked**
   - Date: 2026-09-04.
   - Result: `VERIFIED` for the reported policy/test scope.
   - Files: `plachem_fast_gateway/runtime_policy.py`, `plachem_fast_gateway/core_engine.py`.
   - Evidence: policy and focused evidence reported caller timeout/wait values cannot override server policy.
9. **LOCAL execution/recovery budget baseline**
   - Date: 2026-09-04.
   - Result: `UNVERIFIED`; the historical run was partial and did not complete the intended boundary.
   - Files: `plachem_fast_gateway/runtime_policy.py`, `plachem_fast_gateway/core_engine.py`.
   - Evidence: execution/finalization separation was observed, but complete LIVE acceptance was not recorded.
10. **Runtime Policy V2**
    - Date: 2026-09-04.
    - Result: `VERIFIED` for the reported policy-focused scope.
    - Files: `plachem_fast_gateway/runtime_policy.py`, `plachem_fast_gateway/core_engine.py`.
    - Evidence: Runtime Policy V2 focused/static evidence reported in the 2026-09-04 record.
11. **Phase2 dependency/readiness**
    - Date: 2026-09-05.
    - Result: `VERIFIED` for the reported Phase 2 readiness scope.
    - Files: `war_room_execution_readiness.py`, `war_room_execution_compiler.py`, `war_room_orchestration.py`.
    - Evidence: Phase 2 readiness/dependency record reported prerequisite and duplicate-dispatch guards.
12. **terminal→next READY auto dispatch**
    - Date: 2026-09-05.
    - Result: `VERIFIED` for the reported Phase 2 flow scope.
    - Files: `war_room_orchestration.py`, `war_room_execution_units.py`.
    - Evidence: Phase 2 record reported terminal child transition to the next READY unit.
13. **durable atomic claim**
    - Date: 2026-09-05.
    - Result: `UNVERIFIED`; no exact current acceptance evidence is recorded here.
    - Files: execution-unit/orchestration implementation references.
    - Evidence: no qualifying command/result evidence available in this docs-only task.
14. **independent Core/OpenClaw sessions**
    - Date: 2026-09-05.
    - Result: `UNVERIFIED`; no exact current acceptance evidence is recorded here.
    - Files: Core/OpenClaw adapter implementation references.
    - Evidence: no qualifying command/result evidence available in this docs-only task.
15. **Parent Workflow Finalization**
    - Date: 2026-09-05.
    - Result: `VERIFIED` for the reported Phase 2 finalization scope.
    - Files: `war_room_orchestration.py`, `war_room_execution_units.py`.
    - Evidence: Phase 2 record reported parent outcome and dependency finalization.
16. **Result/Evidence Truth validation**
    - Date: 2026-09-04.
    - Result: `VERIFIED` for the reported historical focused scope; current dirty-tree acceptance remains `UNVERIFIED`.
    - Files: `war_room_fast_gateway.py`, `plachem_fast_gateway/core_engine.py`, `war_room_execution_units.py`.
    - Evidence: closure record reported result/evidence projection with provenance and no timeout false-positive.

## Evidence rule

Historical PASS is scoped to its date, revision, fixture, command, and report. It does not certify the current dirty LIVE tree, runtime, deployment, UI, database, or recovery completion.
