from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "war-room.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "war-room-ui.js").read_text(encoding="utf-8")


def test_uat_navigation_selection_and_filter_contracts() -> None:
    assert "/api/war-room/tasks/${encodeURIComponent(pinnedTaskId)}" in JS
    assert "selectedProjectId = exact.task.project_id" in JS
    assert "currentProjects.unshift" in JS
    assert "sessionStorage.setItem(UI_STATE_KEY" in JS
    assert 'window.addEventListener("popstate"' in JS
    assert 'params.set("project_id", selectedProjectId)' in JS
    assert 'params.set("task_id", selectedTaskId)' in JS
    assert "const visibleTasks = currentTasks.filter" in JS
    assert 'id="task-status-filter" onchange="renderTasks()"' in HTML
    assert 'id="task-search-filter" oninput="renderTasks()"' in HTML


def test_uat_mutation_target_and_single_flight_contracts() -> None:
    assert "async function requireFreshTask" in JS
    assert "async function withMutation" in JS
    assert "mutationInFlight.has(key)" in JS
    assert "data-mutation" in HTML
    assert "await requireFreshTask" in JS
    assert "/api/war-room/tasks/${encodeURIComponent(task.id)}/stop" in JS
    assert "prepareTaskForReapproval" in JS


def test_uat_qa_result_audit_and_worker_failure_contracts() -> None:
    assert 'id="qa-result"' in HTML
    assert "/api/war-room/tasks/${encodeURIComponent(task.id)}/audit" in JS
    assert "Worker 원본 응답" in JS
    assert "정규화 결과" in JS
    assert "거절·실패 사유" in JS
    assert "await load(); await renderReview();" in JS
    assert "latest_qa_verdict?.verdict" in JS
    assert "row.response_body || row.raw_response" in JS
    assert "row.result_json || row.rejected_result_json || row.result_summary" in JS
    assert "row.validation_error || row.error_detail" in JS
    assert "거절된 정규화 결과" in JS


def test_uat_rework_delivery_deduplicates_only_current_revision() -> None:
    assert "Number(row.task_revision || 1) === Number(task.revision || 1)" in JS
    assert "현재 revision에 이미 생성된 delivery" in JS
    assert 'deadline_at:Math.floor(Date.now()/1000)+1800' in JS


def test_invalid_pinned_task_fail_closes_every_mutation_control() -> None:
    assert "function enforcePinnedTaskFailClosed()" in JS
    assert "pinnedTaskId !== null && !taskById(pinnedTaskId)" in JS
    assert '"[data-mutation]"' in JS
    assert '"#task-form button"' in JS
    assert '"#participant-form button"' in JS
    assert '"#message-form button"' in JS
    assert 'control.dataset.pinnedFailClosed = "true"' in JS
    assert "control.hidden = true" in JS
    assert "control.disabled = true" in JS
    assert 'control.setAttribute("aria-disabled", "true")' in JS
    assert "enforcePinnedTaskFailClosed();" in JS
    assert HTML.count('id="qa-result"') == 1


def test_invalid_pinned_lock_restores_accessibility_before_permission_recalculation() -> None:
    apply_access = JS.split("function applyAccess", 1)[1].split("function enforcePinnedTaskFailClosed", 1)[0]
    restore = JS.split("function enforcePinnedTaskFailClosed", 1)[1].split("function taskById", 1)[0]
    assert apply_access.index("enforcePinnedTaskFailClosed();") < apply_access.index("const permissions")
    assert 'control.dataset.pinnedPreviousTitle = control.title || ""' in restore
    assert 'control.getAttribute("aria-disabled")' in restore
    assert 'control.title = control.dataset.pinnedPreviousTitle || ""' in restore
    assert 'control.removeAttribute("aria-disabled")' in restore
    assert "delete control.dataset.pinnedPreviousTitle" in restore
    assert "delete control.dataset.pinnedPreviousAriaDisabled" in restore


def test_uat_participant_edit_and_unimplemented_opinion_are_honest() -> None:
    assert "dataset.editingPrincipal" in JS
    assert "principalSelect.disabled = true" in JS
    assert "Agent 의견 요청 · 미구현" in HTML
    assert "기록만 생성하지 않습니다" in JS
    opinion_branch = JS.split('if (messageType === "opinion")', 1)[1].split("const result", 1)[0]
    assert "throw new Error" in opinion_branch


def test_mutation_contract_binds_project_task_and_revision() -> None:
    assert "function mutationContract(task)" in JS
    assert "contract_version:1" in JS
    assert "project_id:task.project_id || selectedProjectId" in JS
    assert "task_revision:Number(task.revision || 1)" in JS
    assert "_validate_mutation_contract(body, task)" in (ROOT / "war_room_actions.py").read_text(encoding="utf-8")
    assert "mutation project binding is stale" in (ROOT / "war_room_actions.py").read_text(encoding="utf-8")
    assert "mutation task revision is stale" in (ROOT / "war_room_actions.py").read_text(encoding="utf-8")


def test_no_task_selection_disables_mutations_with_guidance() -> None:
    assert 'id="task-selection-hint"' in HTML
    assert 'data-requires-task' in HTML
    assert "function enforceTaskSelectionState()" in JS
    assert 'control.disabled = true' in JS.split("function enforceTaskSelectionState", 1)[1].split("function taskById", 1)[0]
    assert 'control.title = "먼저 작업을 선택하세요"' in JS
    assert '작업을 선택하면 승인·실행·선택 작업 중지·재작업 버튼이 활성화됩니다.' in JS
    assert HTML.count('data-requires-task') >= 8
    assert 'document.querySelectorAll("[data-requires-task]")' in JS


def test_load_discards_stale_project_responses_and_mobile_layout_is_bounded() -> None:
    assert "let loadGeneration = 0" in JS
    assert "const generation = ++loadGeneration" in JS
    assert "generation !== loadGeneration || projectId !== selectedProjectId" in JS
    assert "html,body { width:100%; max-width:100%; overflow-x:hidden; }" in HTML
    assert ".project-list .project{min-width:0" in HTML
    assert ".two-col,.review-grid" in HTML
