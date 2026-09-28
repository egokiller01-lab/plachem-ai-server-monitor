let selectedProjectId = null;
let selectedDocumentVersion = null;
let projectAccess = {permissions: []};
let currentProject = null;
let currentProjects = [];
let currentParticipants = [];
let currentAgentCatalog = [];
let currentTasks = [];
let currentDocuments = [];
let currentDeliveries = [];
let currentProcessBoard = null;
let currentReadiness = null;
let demoMode = false;
let quickTaskId = null;
let selectedTaskId = null;
let pinnedTaskId = new URLSearchParams(window.location.search).get("task_id");
let currentScreen = new URLSearchParams(window.location.search).get("screen") || "dashboard";
let pinnedLookupError = null;
const mutationInFlight = new Set();
const UI_STATE_KEY = "war-room-ui-state-v2";
let loadGeneration = 0;

const esc = value => String(value ?? "").replace(
  /[&<>"']/g,
  ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]),
);

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${response.status} ${data.detail || response.statusText}`);
  return data;
}

const get = url => requestJson(url);
const post = (url, body, method = "POST") => requestJson(url, {
  method,
  headers: {"Content-Type":"application/json", "Idempotency-Key":crypto.randomUUID()},
  body: JSON.stringify(body),
});

async function freshMutationContext(action, targetId = null) {
  if (!selectedProjectId) throw new Error("프로젝트를 다시 선택하세요");
  const params = new URLSearchParams({action});
  if (targetId) params.set("target_id", targetId);
  return get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/mutation-context?${params}`);
}

async function guardedPost(url, body, action, targetId = null, method = "POST") {
  const context = await freshMutationContext(action, targetId);
  return post(url, {...(body || {}), context_token:context.context_token}, method);
}

function mutationContract(task) {
  return {contract_version:1, project_id:task.project_id || selectedProjectId,
    task_id:task.id, task_revision:Number(task.revision || 1)};
}

function fail(id, error) {
  const target = document.getElementById(id);
  if (target) target.innerHTML = `<div class="empty error">조회 실패: ${esc(error.message)}</div>`;
}

function storedUiState() {
  try { return JSON.parse(sessionStorage.getItem(UI_STATE_KEY) || "{}"); }
  catch (_) { return {}; }
}

function saveUiState() {
  sessionStorage.setItem(UI_STATE_KEY, JSON.stringify({
    projectId: selectedProjectId,
    taskId: selectedTaskId,
    screen: currentScreen,
    statusFilter: document.getElementById("task-status-filter")?.value || "",
    searchFilter: document.getElementById("task-search-filter")?.value || "",
  }));
}

function updateLocation({push = false} = {}) {
  const params = new URLSearchParams(window.location.search);
  if (selectedProjectId) params.set("project_id", selectedProjectId); else params.delete("project_id");
  if (selectedTaskId) params.set("task_id", selectedTaskId); else if (!pinnedTaskId) params.delete("task_id");
  params.set("screen", currentScreen);
  const url = `${window.location.pathname}?${params}`;
  window.history[push ? "pushState" : "replaceState"]({projectId:selectedProjectId, taskId:selectedTaskId, screen:currentScreen}, "", url);
  pinnedTaskId = params.get("task_id");
  saveUiState();
}

function navigateScreen(name, {push = true} = {}) {
  if (name === "review") name = "task";
  currentScreen = name;
  document.querySelectorAll(".screen").forEach(screen => { screen.hidden = screen.id !== `screen-${name}`; });
  document.querySelectorAll("[data-screen]").forEach(button => button.setAttribute("aria-current", button.dataset.screen === name ? "page" : "false"));
  if (name === "task") renderTaskDetail();
  if (name === "task") renderReview();
  if (name === "process-board") renderProcessBoard();
  if (name === "documents") renderDocuments();
  if (push) updateLocation({push:true});
}

function processBoardText(value, fallback = "-") {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.length ? value.map(item => processBoardText(item)).join(", ") : fallback;
  if (typeof value === "object") return processBoardText(value.session_id || value.id || value.name, fallback);
  return String(value);
}

function renderReadinessBanner(readiness = currentReadiness) {
  const target = document.getElementById("process-board-readiness");
  if (!target) return;
  if (!readiness || readiness.mode === "unavailable") {
    target.className = "readiness-banner unavailable";
    target.textContent = "준비 상태를 확인할 수 없습니다. 대표 완료 상태로 간주하지 않습니다.";
    return;
  }
  const blocking = (readiness.blocking_task_ids || []).map(esc).join(", ") || "없음";
  const reasons = readiness.blocking_reasons || {};
  const reasonLabels = {
    CURRENT_EVIDENCE: "현재 revision Evidence 없음",
    SIGNED_QA_PASS: "현재 revision QA PASS 없음",
    SESSION_INTEGRITY: "Session Integrity 미충족",
    REPRESENTATIVE_APPROVAL: "대표 승인 이력 없음",
    GROUNDING_PACKET_INVALID: "Grounding packet 무결성 오류",
    QA_SIGNATURE_UNAVAILABLE: "QA 서명 검증 불가",
    TASK_NOT_IN_QA: "QA 상태 아님",
  };
  const reasonSummary = Object.entries(reasons).map(([taskId, codes]) => {
    const labels = (Array.isArray(codes) ? codes : []).map(code => reasonLabels[code] || code);
    return `${esc(taskId)} [${labels.map(esc).join(", ")}]`;
  }).join(" · ");
  const superseded = (readiness.nonblocking_superseded_ids || []).length;
  if (readiness.ready_for_representative_completion === true) {
    target.className = "readiness-banner ready";
    target.innerHTML = `<strong>대표 완료 준비됨</strong> · 활성 task ${Number(readiness.considered_task_count || 0)}개가 완료되었거나 실제 대표 완료 승인 조건을 충족합니다. SUPERSEDED 이력 ${superseded}개는 차단하지 않습니다. (상태 조회만 수행)`;
  } else {
    target.className = "readiness-banner blocked";
    target.innerHTML = `<strong>대표 완료 준비 안 됨</strong> · 차단 task: ${blocking}.${reasonSummary ? ` 사유: ${reasonSummary}.` : ""} SUPERSEDED 이력 ${superseded}개는 차단하지 않습니다.`;
  }
}

function renderProcessBoard(board = currentProcessBoard) {
  const target = document.getElementById("process-board");
  if (!target || !board) return;
  renderReadinessBanner();
  const states = board.states || ["WAITING","READY","RUNNING","PASS","FAIL","REWORK","BLOCKED","DONE"];
  const columns = board.columns || {};
  const items = board.items || [];
  const summary = states.map(state => `<div class="process-board-state ${state.toLowerCase()}" data-board-state="${state}"><strong>${(columns[state] || []).length}</strong><small>${state}</small></div>`).join("");
  const rows = items.map((item, index) => {
    const state = processBoardText(item.mapped_state);
    const emphasis = ["RUNNING","FAIL","REWORK","BLOCKED"].includes(state) ? ` emphasis-${state.toLowerCase()}` : "";
    const step = processBoardText(item.step_name, processBoardText(item.input, processBoardText(item.step_id)));
    const session = item.session && typeof item.session === "object" ? processBoardText(item.session.session_id || item.session.id) : processBoardText(item.session);
    return `<tr class="${emphasis}" data-board-task-id="${esc(item.task_id)}"><td class="order">${index + 1}</td><td><strong>${esc(processBoardText(item.task_id))}</strong><span class="subline">${esc(step)}</span></td><td>${esc(processBoardText(item.assigned_agent, "미지정"))}</td><td>${statusChip(state)}</td><td>${esc(processBoardText(item.predecessor_step))}</td><td>${esc(session)}</td><td>${esc(processBoardText(item.rework_count, "0"))}</td></tr>`;
  }).join("");
  target.innerHTML = `<div class="process-board-summary" aria-label="Process Board 상태별 작업 수">${summary}</div><div class="muted">총 ${items.length}개 task · API mode ${esc(processBoardText(board.mode))}</div><div class="process-board-table-wrap"><table class="process-board-table"><thead><tr><th scope="col">순서</th><th scope="col">Task / Step</th><th scope="col">Agent</th><th scope="col">Status</th><th scope="col">Predecessor</th><th scope="col">Session</th><th scope="col">Rework</th></tr></thead><tbody>${rows || '<tr><td colspan="7"><div class="empty">표시할 작업 없음</div></td></tr>'}</tbody></table></div>`;
  const updated = document.getElementById("process-board-updated");
  if (updated) updated.textContent = `읽기 전용 · ${new Date().toLocaleTimeString()}`;
}

function openTask(id, screen = "task", {push = true} = {}) {
  selectedTaskId = id; quickTaskId = id;
  ["message-task", "approval-task", "qa-task"].forEach(selectId => { const select = document.getElementById(selectId); if (select) select.value = id; });
  navigateScreen(screen, {push}); renderTaskDetail(); renderReview();
}

function setSelectOptions(id, html, preferredValue = null) {
  const node = document.getElementById(id); if (!node) return;
  const previous = preferredValue ?? node.value;
  node.innerHTML = html;
  if ([...node.options].some(option => option.value === previous)) node.value = previous;
}

async function withMutation(key, outputId, action) {
  if (mutationInFlight.has(key)) return;
  mutationInFlight.add(key);
  document.querySelectorAll("[data-mutation]").forEach(node => { node.disabled = true; node.setAttribute("aria-busy", "true"); });
  try { return await action(); }
  catch (error) { const out = document.getElementById(outputId); if (out) out.textContent = error.message; throw error; }
  finally {
    mutationInFlight.delete(key);
    document.querySelectorAll("[data-mutation]").forEach(node => { node.removeAttribute("aria-busy"); });
    applyAccess();
  }
}

async function requireFreshTask(id, allowedStatuses = null) {
  if (!id || !selectedProjectId) throw new Error("작업과 프로젝트를 선택하세요");
  const data = await get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/tasks`);
  const task = (data.items || []).find(row => row.id === id);
  if (!task) throw new Error(`선택한 작업이 현재 프로젝트에 없습니다 · ${id}`);
  if (allowedStatuses && !allowedStatuses.includes(task.status)) throw new Error(`작업 상태가 변경되었습니다 · ${statusLabel(task.status)}`);
  currentTasks = data.items || [];
  selectedTaskId = id; quickTaskId = id;
  return task;
}

function selectedCurrentTask() { return taskById(selectedTaskId || quickTaskId); }

function statusLabel(status) {
  return ({draft:"초안",awaiting_approval:"승인 대기",approved:"승인됨",running:"수행 중",qa:"QA 대기",completed:"완료",rework_required:"재작업",system_error:"SYSTEM ERROR",stopped:"중지",planning:"기획",active:"진행 중",paused:"일시중지",archived:"보관"})[status] || status || "확인 필요";
}

function statusChip(status) { return `<span class="status-chip ${esc(status)}">${esc(statusLabel(status))}</span>`; }

function renderDashboard(projects = currentProjects, operations = {agents:[]}) {
  const counts = {running:0,qa:0,rework:0,system_error:0,approval:0,completed:0,total:0};
  currentTasks.forEach(task => { counts.total += 1; if (task.status === "running") counts.running += 1; if (task.status === "qa") counts.qa += 1; if (task.status === "rework_required") counts.rework += 1; if (task.state === "system_error") counts.system_error += 1; if (task.status === "awaiting_approval") counts.approval += 1; if (task.status === "completed") counts.completed += 1; });
  const progress = counts.total ? Math.round((counts.completed / counts.total) * 100) : 0;
  document.getElementById("dashboard-kpis").innerHTML = [["진행 중",counts.running],["QA 대기",counts.qa],["재작업",counts.rework],["SYSTEM ERROR",counts.system_error],["대표 승인 대기",counts.approval],["전체 진행률",`${progress}%`]].map(([label,value]) => `<div class="stat"><strong>${esc(value)}</strong><small>${esc(label)} · 작업 ${counts.total}건 / 프로젝트 ${projects.length}건</small></div>`).join("");
  const attention = currentTasks.filter(task => ["qa","rework_required","system_error","awaiting_approval","stopped"].includes(task.state || task.status));
  document.getElementById("dashboard-attention").innerHTML = attention.map(task => { const state = task.state || task.status; const next = state === "system_error" ? "다음 행동: 오류 상세 확인 후 자동 재시도 또는 수동 재전송" : task.status === "rework_required" ? "다음 행동: 재작업 지시" : task.status === "qa" ? "다음 행동: ERPqa 독립 판정" : task.status === "awaiting_approval" ? "다음 행동: 승인 검토" : "다음 행동: 중지 원인 확인"; return `<button class="project" onclick="openTask('${esc(task.id)}')"><strong>${esc(task.scope)}</strong><div>${statusChip(state)} · ${esc(task.assignee_agent_id || "담당 미지정")}</div><small>${next}</small></button>`; }).join("") || '<div class="empty">주의 작업 없음</div>';
  document.getElementById("dashboard-agents").innerHTML = (operations.agents || []).map(agent => `<div class="project"><div><strong>${esc(agent.agent_id)}</strong> ${statusChip(agent.state)}</div><small>세션 ${agent.session_count} · 최근 ${esc(agent.latest?.status || "없음")}</small></div>`).join("") || '<div class="empty">참여 에이전트 상태 없음</div>';
  const monitor = document.getElementById("monitor-banner");
  if (monitor) { monitor.hidden = !operations.degraded; monitor.innerHTML = operations.degraded ? `<strong>AI Server Monitor 장애</strong><br>마지막 확인 ${esc(new Date((operations.last_checked || 0) * 1000).toLocaleString())} · 마지막 정상 ${esc(new Date((operations.last_good_snapshot?.captured_at || 0) * 1000).toLocaleString())}<br>마지막 정상 상태를 표시 중입니다. 새 호출 전 서버 상태를 재조회하세요.` : ""; }
  const monitorLast = document.getElementById("monitor-last-good");
  if (monitorLast) monitorLast.textContent = `마지막 확인 ${new Date((operations.last_checked || 0) * 1000).toLocaleString()} · 정상 스냅샷 ${operations.last_good_snapshot ? "보존됨" : "없음"}`;
  document.getElementById("dashboard-updated").textContent = `마지막 갱신 ${new Date().toLocaleTimeString()}`;
}

function renderProjectDetail() {
  if (!currentProject) return;
  document.getElementById("project-title").textContent = currentProject.name;
  document.getElementById("project-status-chip").innerHTML = statusChip(currentProject.status);
  document.getElementById("project-goal").textContent = `목표: ${currentProject.name}의 요구사항·작업·QA·승인 흐름을 한 곳에서 관리`;
  document.getElementById("project-meta").textContent = `참여자 ${currentParticipants.length}명 · 생성 ${new Date(currentProject.created_at * 1000).toLocaleString()} · 갱신 ${new Date(currentProject.updated_at * 1000).toLocaleString()}`;
  const statuses = new Set(currentTasks.map(task => task.status));
  document.querySelectorAll("#project-stage div").forEach((node,index) => { node.classList.toggle("done", index === 0 || (index === 1 && currentTasks.length > 0)); node.classList.toggle("current", (index === 2 && statuses.has("qa")) || (index === 3 && statuses.has("completed"))); });
}

function renderTaskDetail() {
  const task = selectedCurrentTask(); const target = document.getElementById("task-detail");
  if (!task) { target.innerHTML = '<div class="empty">프로젝트에서 작업을 선택하면 목표·담당·완료조건이 표시됩니다.</div>'; document.getElementById("task-status").innerHTML = ""; return; }
  document.getElementById("task-screen-title").textContent = `작업 관제 · ${task.scope}`;
  document.getElementById("task-detail-subtitle").textContent = `담당 ${task.assignee_agent_id || "미지정"} · 검수 ${task.reviewer_agent_id || "미지정"} · 참고 ${task.document_version || "-"} · revision ${task.revision}`;
  const state = task.state || task.status;
  document.getElementById("task-status").innerHTML = statusChip(state);
  target.innerHTML = `<div class="three-col"><div><small class="muted">지시 원문·범위</small><p>${esc(task.instruction_body || task.scope)}</p></div><div><small class="muted">담당·독립 검수</small><p>${esc(task.assignee_agent_id || "-")} → ${esc(task.reviewer_agent_id || "-")}</p></div><div><small class="muted">완료조건</small><p>결과 제출 → 필수 evidence → 독립 QA PASS → 대표 승인</p></div></div><div class="muted">대상: ${esc((task.agent_ids || []).join(", ") || "-")} · 상태 원인: ${state === "system_error" ? "전달/서버 오류. QA FAIL·REWORK와 독립 상태" : task.status === "rework_required" ? "QA FAIL/REWORK 또는 결과 검증 실패" : task.status === "stopped" ? "중지 확인됨" : task.status === "stop_unconfirmed" ? "중지 미확인 또는 부분 중지" : "서버 상태 전이"}</div>`;
  const legacyForm = document.getElementById("legacy-reviewer-form");
  if (legacyForm) {
    legacyForm.hidden = Boolean(task.reviewer_agent_id) || task.status === "completed" || projectAccess.is_representative !== true;
    const select = document.getElementById("legacy-reviewer");
    const executors = new Set(task.agent_ids || []);
    select.innerHTML = '<option value="">독립 검수자 선택</option>' + currentParticipants
      .filter(row => row.active && row.role === "qa" && !executors.has(row.principal_id))
      .map(row => `<option value="${esc(row.principal_id)}">${esc(row.principal_id)}</option>`).join("");
  }
  renderExecutionProjection(task);
  loadTaskDetailJev(task.id);
  loadTaskResultVerifier(task.id);
}

document.getElementById("legacy-reviewer-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const task = selectedCurrentTask();
  if (!task) return;
  const out = document.getElementById("legacy-reviewer-result");
  try {
    const result = await post(`/api/war-room/tasks/${encodeURIComponent(task.id)}/reviewer`, {
      reviewer_agent_id: document.getElementById("legacy-reviewer").value,
      reason: document.getElementById("legacy-reviewer-reason").value,
      task_revision: Number(task.revision),
    });
    out.textContent = `검수자 ${result.reviewer_agent_id} 지정 · 기존 유효 승인 ${result.revoked_approval_ids.length}건 해제 · 새 승인 필요`;
    document.getElementById("legacy-reviewer-reason").value = "";
    await load();
  } catch (error) { out.textContent = `검수자 지정 실패 · ${error.message}`; }
});

function humanFailureProjection(row) {
  const code = row.validation_error || row.error_code || row.cancel_reason || "";
  if (!code && row.run_status !== "FAIL" && !["failed","timed_out"].includes(row.status)) return "";
  let stage = "작업 처리";
  let cause = "실행 또는 검증 단계에서 오류가 발생했습니다.";
  let next = "원인을 확인한 뒤 재승인·재실행하십시오.";
  if (String(code).includes("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED")) {
    stage = "FastGateway 결과 검증";
    cause = "Worker 응답은 도착했지만 Evidence를 실제 실행 기록에서 검증하지 못해 결과가 거절됐습니다.";
    next = "Evidence 검증 조건을 확인한 뒤 재승인·재실행하십시오.";
  } else if (String(code).includes("EVIDENCE_CONTRADICTION")) {
    stage = "FastGateway 결과 검증";
    cause = "Worker 보고 내용과 실제 실행 기록이 서로 모순됩니다.";
    next = "Worker 원본 응답과 실행 기록을 비교해 재작업하십시오.";
  } else if (row.status === "timed_out") {
    stage = "Agent 실행";
    cause = "Agent가 제한시간 안에 최종 응답을 반환하지 못했습니다.";
    next = "Agent 상태를 확인하고 재실행하거나 담당 Agent를 변경하십시오.";
  }
  const worker = row.response_body || row.raw_response || row.result_summary ? "Worker 응답 수신 완료" : "Worker 응답 완료 확인 안 됨";
  return `<div class="error-detail"><strong>사람이 읽는 실패 원인</strong><br><b>${esc(stage)}</b> · ${esc(cause)}<br><small>${esc(worker)} · 다음 조치: ${esc(next)}</small>${code ? `<details><summary>기술 코드</summary><code>${esc(code)}</code></details>` : ""}</div>`;
}

function renderExecutionProjection(task) {
  const target = document.getElementById("task-detail");
  const rows = currentDeliveries.filter(row => row.task_id === task.id || row.message_id === task.source_message_id);
  const fast = rows.filter(row => row.execution_mode === "FAST_GATEWAY" || row.core_run_id);
  if (!fast.length) return;
  const refs = value => { try { const parsed = JSON.parse(value || "[]"); return Array.isArray(parsed) ? parsed.map(item => typeof item === "string" ? item : (item?.path || item?.detail || JSON.stringify(item))).join(", ") : "-"; } catch (_) { return value || "-"; } };
  target.insertAdjacentHTML("beforeend", `<div class="card" style="margin-top:12px"><strong>Fast Gateway 실행 투영</strong>${fast.map(row => `<div class="muted">Agent ${esc(row.agent_id)} · Core ${esc(row.core_run_id || "-")} · 실행 ${esc(row.run_status || row.status)} · runtime ${esc(row.runtime_seconds ?? "-")}s · policy ${esc(row.policy_status || "-")} · escalation ${row.escalation_required ? "required" : "no"}${row.cancel_reason ? ` · cancel ${esc(row.cancel_reason)}` : ""}<br>결과 요약: ${esc(row.result_summary || "-")}<br>Evidence: ${esc(refs(row.evidence_json))}<br>Artifacts: ${esc(refs(row.artifacts_json))}</div>`).join("")}</div>`);
}

async function renderReview() {
  const task = selectedCurrentTask(); if (!task) return;
  const deliveries = currentDeliveries.filter(row => row.task_id === task.id || row.message_id === task.source_message_id);
  const parseRefs = value => { try { const parsed = typeof value === "string" ? JSON.parse(value || "null") : value; return parsed ? JSON.stringify(parsed, null, 2) : "-"; } catch (_) { return value || "-"; } };
  document.getElementById("coder-result").innerHTML = `<p>${esc(task.scope)}</p><div class="muted">상태 ${statusChip(task.status)}</div>${deliveries.map(row => { const raw = row.response_body || row.raw_response; const normalized = row.result_json || row.rejected_result_json || row.result_summary; const failure = row.validation_error || row.error_detail; return `<article class="project"><strong>${esc(row.agent_id)} · ${esc(deliveryLabel(row.status))}</strong><small>task ${esc(task.id)} · run ${esc(row.run_id || row.core_run_id || "-")}</small>${humanFailureProjection(row)}${raw ? `<details><summary>Worker 원본 응답</summary><pre>${esc(raw)}</pre></details>` : '<div class="warning">Worker 원본 응답 미보존</div>'}${normalized ? `<details><summary>${row.rejected_result_json && !row.result_json ? "거절된 정규화 결과" : "정규화 결과"}</summary><pre>${esc(parseRefs(normalized))}</pre></details>` : ""}${row.evidence_json ? `<details><summary>Evidence</summary><pre>${esc(parseRefs(row.evidence_json))}</pre></details>` : ""}${row.artifacts_json ? `<details><summary>Artifacts</summary><pre>${esc(parseRefs(row.artifacts_json))}</pre></details>` : ""}${row.error_code || failure ? `<div class="error-detail"><strong>거절·실패 사유</strong><br>${esc(row.error_code || "RESULT_VALIDATION_FAILED")}${failure ? `<br>${esc(failure)}` : ""}</div>` : ""}</article>`; }).join("") || '<div class="empty">이 작업의 실행 결과가 없습니다.</div>'}`;
  document.getElementById("qa-task").value = task.id;
  try {
    const [evidence, audit] = await Promise.all([
      get(`/api/war-room/tasks/${encodeURIComponent(task.id)}/evidence`),
      get(`/api/war-room/tasks/${encodeURIComponent(task.id)}/audit`),
    ]);
    document.getElementById("evidence-list").innerHTML = evidence.items.map(row => `<div class="evidence"><strong>${esc(row.evidence_type)}</strong> · ${esc(row.summary)}<br><small>${esc(row.uri)} · revision ${esc(row.task_revision)}</small></div>`).join("") || '<div class="empty">필수 증거 없음</div>';
    const verdict = task.latest_qa_verdict ? `${task.latest_qa_verdict.verdict} · ${task.latest_qa_verdict.qa_principal}` : "판정 없음";
    document.getElementById("qa-result-summary").innerHTML = `<p>최근 ERPqa 판정: <strong>${esc(verdict)}</strong></p><p class="muted">필수 증거 ${task.evidence_count || 0}건 · QA cycle ${task.qa_cycle}</p>`;
    const canComplete = task.status === "qa" && task.latest_qa_verdict?.verdict === "PASS" && Number(task.evidence_count || 0) > 0;
    document.getElementById("representative-complete").disabled = !canComplete;
    document.getElementById("representative-explanation").textContent = canComplete ? "필수 evidence와 ERPqa PASS가 확인되어 대표 승인할 수 있습니다." : "QA PASS와 필수 증거 전에는 승인 불가";
    document.getElementById("review-guard").textContent = canComplete ? "대표 승인 가능" : "QA PASS와 필수 증거 전에는 승인 불가";
    renderAudit(audit.items || []);
  } catch (error) { fail("evidence-list", error); }
}

async function completeSelectedTask() {
  const task = selectedCurrentTask(); if (!task) return;
  try { await withMutation(`complete:${task.id}`, "representative-result", async () => { const fresh = await requireFreshTask(task.id, ["qa"]); if (fresh.latest_qa_verdict?.verdict !== "PASS" || Number(fresh.evidence_count || 0) < 1) throw new Error("QA PASS와 필수 증거를 다시 확인하세요"); const result = await guardedPost(`/api/war-room/tasks/${fresh.id}/representative-completion`, {decision:"approved"}, "representative_completion", fresh.id); document.getElementById("representative-result").textContent = `대표 승인 ${result.status || "완료"}`; await load(); }); }
  catch (_) { /* reported */ }
}

function applyAccess(access = projectAccess) {
  projectAccess = access;
  // First remove a previous invalid-pinned lock. The permission pass below
  // then recalculates the current actor's real disabled reason and title.
  enforcePinnedTaskFailClosed();
  const permissions = new Set(access.permissions || []);
  document.querySelectorAll("[data-permission]").forEach(element => {
    const allowed = permissions.has(element.dataset.permission);
    const controls = element.matches("button,input,select,textarea")
      ? [element]
      : [...element.querySelectorAll("button,input,select,textarea")];
    controls.forEach(control => {
      control.disabled = !allowed;
      control.setAttribute("aria-disabled", String(!allowed));
      control.title = allowed ? "" : `${access.role || "observer"} 권한으로 사용할 수 없습니다`;
    });
  });
  document.querySelectorAll("[data-role]").forEach(control => {
    const allowed = access.role === control.dataset.role;
    control.disabled = !allowed;
    control.setAttribute("aria-disabled", String(!allowed));
  });
  document.querySelectorAll("[data-representative]").forEach(control => {
    const allowed = access.is_representative === true;
    control.disabled = !allowed;
    control.setAttribute("aria-disabled", String(!allowed));
    control.title = allowed ? "" : "대표 승인 권한이 필요합니다";
  });
  document.getElementById("audit-summary").textContent =
    `principal ${access.principal_id || "-"} · role ${access.role || "-"} · permissions ${[...permissions].join(", ")}`;
  enforcePinnedTaskFailClosed();
  enforceTaskSelectionState();
}

function enforceTaskSelectionState() {
  const selectValues = ["approval-task", "qa-task"].map(id => document.getElementById(id)?.value || "");
  const selectedId = selectedTaskId || quickTaskId || selectValues.find(Boolean) || null;
  const selected = Boolean(selectedId && taskById(selectedId));
  const hint = document.getElementById("task-selection-hint");
  if (hint) {
    hint.hidden = selected;
    hint.textContent = selected
      ? ""
      : "작업을 선택하면 승인·실행·선택 작업 중지·재작업 버튼이 활성화됩니다.";
  }
  document.querySelectorAll("[data-requires-task]").forEach(control => {
    if (!selected) {
      control.disabled = true;
      control.setAttribute("aria-disabled", "true");
      control.title = "먼저 작업을 선택하세요";
      control.dataset.taskSelectionDisabled = "true";
      return;
    }
    if (control.dataset.taskSelectionDisabled === "true") {
      delete control.dataset.taskSelectionDisabled;
      const permissionAllowed = !control.dataset.permission || (projectAccess.permissions || []).includes(control.dataset.permission);
      const roleAllowed = !control.dataset.role || projectAccess.role === control.dataset.role;
      const representativeAllowed = !control.dataset.representative || projectAccess.is_representative === true;
      control.disabled = !(permissionAllowed && roleAllowed && representativeAllowed);
      control.setAttribute("aria-disabled", String(control.disabled));
      if (!control.disabled) control.title = "";
    }
  });
}

function enforcePinnedTaskFailClosed() {
  const unavailable = pinnedTaskId !== null && !taskById(pinnedTaskId);
  const selectors = [
    "[data-mutation]", "#quick-prepare", "#quick-open-qa", "#representative-complete",
    "#new-project-button", "#project-form button", "#project-edit-form button",
    "#task-form button", "#participant-form button", "#legacy-reviewer-form button",
    "#message-form button", 'button[onclick^="retryDelivery("]',
  ].join(",");
  document.querySelectorAll(selectors).forEach(control => {
    if (unavailable) {
      if (control.dataset.pinnedFailClosed !== "true") {
        control.dataset.pinnedPreviousHidden = String(control.hidden);
        control.dataset.pinnedPreviousDisabled = String(control.disabled);
        control.dataset.pinnedPreviousTitle = control.title || "";
        control.dataset.pinnedPreviousAriaDisabled = control.getAttribute("aria-disabled") || "";
      }
      control.dataset.pinnedFailClosed = "true";
      control.hidden = true;
      control.disabled = true;
      control.setAttribute("aria-disabled", "true");
      control.title = `존재하지 않는 고정 task에서는 변경할 수 없습니다 · ${pinnedTaskId}`;
    } else if (control.dataset.pinnedFailClosed === "true") {
      control.hidden = control.dataset.pinnedPreviousHidden === "true";
      control.disabled = control.dataset.pinnedPreviousDisabled === "true";
      control.title = control.dataset.pinnedPreviousTitle || "";
      if (control.dataset.pinnedPreviousAriaDisabled) control.setAttribute("aria-disabled", control.dataset.pinnedPreviousAriaDisabled);
      else control.removeAttribute("aria-disabled");
      delete control.dataset.pinnedFailClosed;
      delete control.dataset.pinnedPreviousHidden;
      delete control.dataset.pinnedPreviousDisabled;
      delete control.dataset.pinnedPreviousTitle;
      delete control.dataset.pinnedPreviousAriaDisabled;
    }
  });
}

function taskById(id) {
  return currentTasks.find(task => task.id === id);
}

function selectedTask(selectId) {
  const task = taskById(document.getElementById(selectId).value);
  if (!task) throw new Error("작업을 선택하세요");
  return task;
}


function resetDocumentForm() {
  document.getElementById("document-id").value = "";
  document.getElementById("document-title").value = "";
  document.getElementById("document-uri").value = "";
  document.getElementById("document-summary").value = "";
  document.getElementById("document-expected-version").value = "";
  document.getElementById("document-category").value = "requirements";
  document.getElementById("document-relation").value = "output";
  document.getElementById("document-task").value = "";
  document.getElementById("document-result").textContent = "";
}

function editDocumentVersion(documentId) {
  const row = currentDocuments.find(item => item.id === documentId);
  if (!row) return;
  document.getElementById("document-id").value = row.id;
  document.getElementById("document-title").value = row.title || "";
  document.getElementById("document-category").value = row.category || "other";
  document.getElementById("document-uri").value = row.uri || "";
  document.getElementById("document-summary").value = row.summary || "";
  document.getElementById("document-expected-version").value = row.current_version || "";
  navigateScreen("documents");
  document.getElementById("document-uri").focus();
}

async function showDocumentHistory(documentId) {
  const target = document.getElementById("document-history");
  target.innerHTML = '<div class="empty">버전 이력 불러오는 중…</div>';
  try {
    const data = await get(`/api/war-room/documents/${encodeURIComponent(documentId)}/versions`);
    target.innerHTML = (data.items || []).map(row =>
      `<div class="project"><strong>v${esc(row.version)} · ${esc(row.sha256?.slice(0,12) || "-")}</strong><div>${esc(row.uri)}</div><small>${esc(row.source_agent_id || "system")} · task ${esc(row.source_task_id || "project")} · session ${esc(row.source_session_id || "-")}</small><small>${esc(row.summary || "")}</small></div>`
    ).join("") || '<div class="empty">버전 없음</div>';
  } catch (error) { fail("document-history", error); }
}

function renderDocuments() {
  const target = document.getElementById("documents");
  if (!target) return;
  const category = document.getElementById("document-category-filter")?.value || "";
  const search = (document.getElementById("document-search-filter")?.value || "").trim().toLowerCase();
  const visible = currentDocuments.filter(row => {
    if (category && row.category !== category) return false;
    if (!search) return true;
    return [row.title, row.summary, row.uri, row.category].some(value => String(value || "").toLowerCase().includes(search));
  });
  document.getElementById("documents-count").textContent = `${currentDocuments.length} documents`;
  target.innerHTML = visible.map(row =>
    `<div class="project"><button class="project" onclick="showDocumentHistory('${esc(row.id)}')"><strong>${esc(row.title)}</strong><div>${esc(row.category)} · v${esc(row.current_version)} · ${esc(row.status)}</div><small>${esc(row.uri)}</small><small>작성 ${esc(row.source_agent_id || row.created_by || "system")} · task ${esc(row.source_task_id || "project")}</small></button><div class="filterbar"><button data-permission="manage" onclick="editDocumentVersion('${esc(row.id)}')">새 버전 등록</button></div></div>`
  ).join("") || '<div class="empty">조건에 맞는 프로젝트 문서 없음</div>';
  const taskOptions = '<option value="">Project 공용</option>' + currentTasks.map(task =>
    `<option value="${esc(task.id)}">${esc(task.scope.slice(0,48))}</option>`
  ).join("");
  setSelectOptions("document-task", taskOptions, document.getElementById("document-task")?.value || "");
  applyAccess();
}

function renderTasks() {
  if (pinnedTaskId !== null) {
    // A supplied task_id is an explicit safety boundary: never fall back to
    // the most recent task when it is absent or invalid.
    quickTaskId = currentTasks.some(task => task.id === pinnedTaskId) ? pinnedTaskId : null;
  } else if (!quickTaskId) {
    quickTaskId = currentTasks.find(task => ["awaiting_approval","approved","running","qa"].includes(task.status) && task.source_message_id)?.id
      || currentTasks.find(task => task.status === "completed" && task.source_message_id)?.id || null;
  }
  const statusFilter = document.getElementById("task-status-filter")?.value || "";
  const searchFilter = (document.getElementById("task-search-filter")?.value || "").trim().toLowerCase();
  const visibleTasks = currentTasks.filter(task => {
    if (statusFilter && task.status !== statusFilter) return false;
    if (!searchFilter) return true;
    return [task.id, task.scope, task.instruction_body, task.assignee_agent_id, task.reviewer_agent_id]
      .some(value => String(value || "").toLowerCase().includes(searchFilter));
  });
  document.getElementById("tasks").innerHTML = visibleTasks.map(task => {
    const buttons = [];
    if (task.status === "draft") buttons.push(`<button data-permission="manage" onclick="changeTask('${task.id}','awaiting_approval')">승인 요청</button>`);
    if (task.status === "awaiting_approval") {
      buttons.push(`<button data-permission="approve" data-mutation onclick="selectApprovalTask('${task.id}')">승인 화면에 선택</button>`);
    }
    if (task.status === "approved") {
      buttons.push(`<button data-permission="execute" data-mutation onclick="selectApprovalTask('${task.id}')">실행 준비</button>`);
    }
    if (task.status === "running" && task.source_message_id && !currentDeliveries.some(row => row.task_id === task.id && Number(row.task_revision || 1) === Number(task.revision || 1))) {
      buttons.push(`<button data-permission="execute" data-mutation onclick="deliverTask('${task.id}')">지시 전달</button>`);
    }
    if (["rework_required","stopped","stop_unconfirmed"].includes(task.status)) buttons.push(`<button data-permission="approve" data-mutation onclick="prepareTaskForReapproval('${task.id}')">재승인 준비</button>`);
    if (["approved","running","qa"].includes(task.status)) buttons.push(`<button data-permission="execute" data-mutation onclick="stopTask('${task.id}')">실제 작업 중지</button>`);
    return `<div class="project"><button class="project" onclick="openTask('${esc(task.id)}')"><strong>${esc(task.scope)}</strong><div>${statusChip(task.status)} · ${esc(task.assignee_agent_id || "담당 미지정")}</div><small>호출 ${task.call_limit} · 턴 ${task.turn_limit} · 문서 ${esc(task.document_version)}</small></button><div class="filterbar">${buttons.join("")}<button onclick="requestJevAdvisory('${esc(task.id)}')">JEV advisory</button></div><div id="jev-advisory-${esc(task.id)}" class="jev-advisory" hidden></div></div>`;
  }).join("") || '<div class="empty">조건에 맞는 작업 없음</div>';
  const options = currentTasks.map(task => `<option value="${esc(task.id)}">${esc(task.status)} · ${esc(task.scope.slice(0, 42))}</option>`);
  setSelectOptions("message-task", '<option value="">작업 선택</option>' + options.filter(option => !option.includes("completed") && !option.includes("stopped")).join(""), selectedTaskId);
  setSelectOptions("approval-task", '<option value="">작업 선택</option>' + options.join(""), selectedTaskId);
  setSelectOptions("qa-task", '<option value="">QA 작업 선택</option>' + options.join(""), selectedTaskId);
  saveUiState();
  renderQuickProgress();
  enforceTaskSelectionState();
}

function clearTaskFilters() {
  document.getElementById("task-status-filter").value = "";
  document.getElementById("task-search-filter").value = "";
  renderTasks();
}

function renderQuickProgress() {
  const task = quickTaskId ? taskById(quickTaskId) : null;
  const status = task?.status;
  const prepared = ["awaiting_approval","approved","running","qa","completed","rework_required"].includes(status);
  const running = ["running","qa","completed"].includes(status);
  document.getElementById("quick-step-1")?.classList.toggle("done", Boolean(prepared));
  document.getElementById("quick-step-2")?.classList.toggle("active", Boolean(prepared && !running));
  document.getElementById("quick-step-2")?.classList.toggle("done", Boolean(running));
  document.getElementById("quick-step-3")?.classList.toggle("active", Boolean(running));
  document.getElementById("quick-step-3")?.classList.toggle("done", status === "completed");
  const approve = document.getElementById("quick-approve-run");
  if (approve) {
    approve.hidden = status !== "awaiting_approval";
    approve.disabled = Boolean(pinnedTaskId !== null && (!task || status !== "awaiting_approval"));
  }
  const qaButton = document.getElementById("quick-open-qa");
  if (qaButton) qaButton.hidden = status !== "qa";
  const completeButton = document.getElementById("quick-complete");
  if (completeButton) completeButton.hidden = status !== "qa";
  let lock = document.getElementById("quick-target-lock");
  if (!lock && pinnedTaskId !== null) {
    lock = document.createElement("div");
    lock.id = "quick-target-lock";
    lock.className = "warning";
    document.getElementById("quick-task-form")?.prepend(lock);
  }
  if (lock) {
    lock.hidden = pinnedTaskId === null;
    if (pinnedTaskId !== null) lock.textContent = task
      ? `고정 대상 · task ${task.id} · 담당 ${task.assignee_agent_id} · 호출 ${task.call_limit}회 · 턴 ${task.turn_limit}회 · ${statusLabel(task.status)}`
      : `고정 대상 task ${pinnedTaskId}를 불러오는 중…`;
  }
  const prepare = document.getElementById("quick-prepare");
  if (prepare && pinnedTaskId !== null) {
    prepare.hidden = true;
    prepare.disabled = true;
  }
  if (pinnedTaskId !== null && !task) {
    ["quick-prepare", "quick-approve-run", "quick-open-qa", "quick-complete"].forEach(id => {
      const control = document.getElementById(id);
      if (control) { control.hidden = true; control.disabled = true; }
    });
    if (lock) lock.textContent = pinnedLookupError
      ? `오류: 고정 task 소속 프로젝트 조회 실패 · ${pinnedLookupError}`
      : `오류: 고정 task를 찾을 수 없습니다 · ${pinnedTaskId}`;
  }
  enforceTaskSelectionState();
}

function deliveryLabel(status) {
  return ({queued:"전달 대기",received:"작업 중",responded:"응답 완료",failed:"실패",timed_out:"시간 초과",stopped:"중지"})[status] || status;
}

function renderQuickDeliveries(items) {
  const relevant = quickTaskId ? items.filter(row => row.task_id === quickTaskId) : [];
  document.getElementById("quick-delivery-cards").innerHTML = relevant.map(row =>
    `<div class="project"><strong>${esc(row.agent_id)}</strong> ${statusChip(row.error_class === "system_error" ? "system_error" : row.status)}<div class="${row.status === "responded" ? "status" : "muted"}">${esc(row.error_code === "agent_busy_queued" ? "다른 작업 종료 후 순서대로 실행" : deliveryLabel(row.status))}</div>${row.response_body ? `<p>${esc(row.response_body)}</p>` : ""}${row.error_code && row.error_code !== "agent_busy_queued" ? `<div class="error-detail"><strong>오류 상세</strong><br>${esc(row.error_code)}<br><small>재시도 ${row.retry_count || row.attempt_count || 0}/${row.max_attempts || 3}</small></div>` : ""}<small>${row.error_class === "system_error" ? (row.status === "queued" ? "다음 행동: 자동 재시도 대기" : "다음 행동: 원인 확인 후 수동 재전송 또는 담당자 교체") : row.status === "responded" ? "다음 행동: 결과와 QA 판정을 비교" : "다음 행동: 처리 완료 대기"}</small></div>`
  ).join("") || '<div class="empty">실행 후 에이전트별 결과가 여기에 표시됩니다.</div>';
}

async function refreshOperations() { await load(); }

async function retryDelivery(id) {
  try { await post(`/api/war-room/deliveries/${encodeURIComponent(id)}/retry`, {}); await load(); }
  catch (error) { document.getElementById("approval-result").textContent = `수동 재전송 실패 · ${error.message}`; }
}

function toggleAdvanced(button) {
  const area = document.getElementById("advanced-area");
  area.hidden = !area.hidden;
  document.querySelectorAll("[data-advanced-section]").forEach(section => { section.hidden = area.hidden; });
  button.setAttribute("aria-expanded", String(!area.hidden));
  button.textContent = area.hidden ? "고급 관리 열기" : "고급 관리 닫기";
}

function renderParticipants() {
  document.getElementById("participant-list").innerHTML = currentParticipants.map(row => {
    const nextActive = row.active ? "false" : "true";
    return `<div class="project"><strong>${esc(row.principal_id)}</strong><div>${esc(row.role)} · ${row.active ? "활성" : "비활성"}</div><small>read ${row.can_read ? "Y" : "N"} · comment ${row.can_comment ? "Y" : "N"} · approve ${row.can_approve ? "Y" : "N"} · execute ${row.can_execute ? "Y" : "N"}</small><div class="filterbar"><button data-permission="manage" onclick="editParticipant('${esc(row.principal_id)}')">수정</button><button data-permission="manage" onclick="setParticipantActive('${esc(row.principal_id)}',${nextActive})">${row.active ? "비활성" : "재활성"}</button></div></div>`;
  }).join("") || '<div class="empty">참여자 없음</div>';
}

function renderAgentControls() {
  const remembered = Object.fromEntries(["quick-assignee","task-assignee","quick-reviewer","task-reviewer","participant-principal","message-agent","timeline-author"].map(id => [id, document.getElementById(id)?.value || ""]));
  const rememberedTargets = Object.fromEntries(["quick-agent-targets","task-agent-targets"].map(id => [id, new Set([...document.querySelectorAll(`#${id} input:checked`)].map(input => input.value))]));
  const participating = new Set(currentParticipants.filter(row => row.active).map(row => row.principal_id));
  const executable = currentAgentCatalog.filter(row => row.execution_eligible && participating.has(row.agent_id));
  const reviewers = currentParticipants.filter(row => row.active && row.role === "qa");
  const checkboxes = executable.map((row, index) => `<label><input type="checkbox" value="${esc(row.agent_id)}" ${index === 0 ? "checked" : ""}> ${esc(row.agent_id)}</label>`).join("") || '<span class="warning">실행 가능한 참여 Agent 없음</span>';
  ["quick-agent-targets", "task-agent-targets"].forEach(id => {
    const node = document.getElementById(id); if (!node) return;
    node.innerHTML = `<legend>수행 에이전트</legend>${checkboxes}`;
    if (rememberedTargets[id].size) [...node.querySelectorAll("input")].forEach(input => { input.checked = rememberedTargets[id].has(input.value); });
  });
  const assigneeOptions = executable.map(row => `<option value="${esc(row.agent_id)}">${esc(row.agent_id)}</option>`).join("");
  ["quick-assignee", "task-assignee"].forEach(id => setSelectOptions(id, '<option value="">담당자 선택</option>' + assigneeOptions, remembered[id]));
  const reviewerOptions = reviewers.map(row => `<option value="${esc(row.principal_id)}">${esc(row.principal_id)}</option>`).join("");
  ["quick-reviewer", "task-reviewer"].forEach(id => setSelectOptions(id, '<option value="">독립 검수자 선택</option>' + reviewerOptions, remembered[id]));
  const addable = currentAgentCatalog.filter(row => !currentParticipants.some(item => item.principal_id === row.agent_id));
  const participantSelect = document.getElementById("participant-principal");
  if (participantSelect) {
    const editing = participantSelect.dataset.editingPrincipal;
    const editingRow = editing ? currentAgentCatalog.find(row => row.agent_id === editing) : null;
    const choices = editingRow && !addable.some(row => row.agent_id === editing) ? [editingRow, ...addable] : addable;
    setSelectOptions("participant-principal", choices.map(row => `<option value="${esc(row.agent_id)}">${esc(row.agent_id)}${row.execution_eligible ? " · 실행 가능" : " · 참여만 가능"}</option>`).join(""), editing || remembered["participant-principal"]);
    participantSelect.disabled = Boolean(editing);
  }
  const messageSelect = document.getElementById("message-agent");
  if (messageSelect) setSelectOptions("message-agent", currentParticipants.filter(row => row.active && row.can_comment).map(row => `<option>${esc(row.principal_id)}</option>`).join(""), remembered["message-agent"]);
  const authorSelect = document.getElementById("timeline-author");
  if (authorSelect) setSelectOptions("timeline-author", '<option value="">전체 작성자</option>' + currentParticipants.map(row => `<option>${esc(row.principal_id)}</option>`).join(""), remembered["timeline-author"]);
  const note = document.getElementById("agent-candidate-note");
  if (note) note.textContent = `OpenClaw 등록 ${currentAgentCatalog.length}명 · 현재 실행 가능 ${executable.length}명. 참여 등록은 승인·실행 권한을 자동 부여하지 않습니다.`;
}

function renderAudit(items) {
  document.getElementById("audit-list").innerHTML = items.map(row => `<div class="project"><strong>${esc(row.event_type)}</strong><div>${esc(row.actor_id)} → ${esc(row.target_type)} ${esc(row.target_id)}</div><small>${new Date(row.created_at * 1000).toLocaleString()} · ${esc(row.correlation_id)}</small></div>`).join("") || '<div class="empty">감사 기록 없음</div>';
}

async function loadTimeline() {
  if (!selectedProjectId) return;
  const generation = loadGeneration;
  const projectId = selectedProjectId;
  const params = new URLSearchParams();
  const filters = {message_type:"timeline-type", author_id:"timeline-author", delivery_status:"timeline-delivery"};
  Object.entries(filters).forEach(([key,id]) => {
    const control = document.getElementById(id); if (!control) return;
    const value = control.value;
    if (value) params.set(key, value);
  });
  [["from_ts","timeline-from"],["to_ts","timeline-to"]].forEach(([key,id]) => {
    const control = document.getElementById(id); if (!control) return;
    const value = control.value;
    if (value) params.set(key, String(Math.floor(new Date(value).getTime() / 1000)));
  });
  try {
    const data = await get(`/api/war-room/projects/${encodeURIComponent(projectId)}/timeline?${params}`);
    if (generation !== loadGeneration || projectId !== selectedProjectId) return;
    document.getElementById("timeline").innerHTML = data.items.map(row => {
      const statuses = row.delivery_statuses ? ` · ${esc(row.delivery_statuses)}` : "";
      const time = new Date(row.created_at * 1000).toLocaleString();
      return `<article class="event"><div class="event-type">${esc(row.message_type)}</div><div><strong>${esc(row.author_id)}</strong><p>${esc(row.body)}</p><small>${time}${statuses}</small></div></article>`;
    }).join("") || '<div class="empty">기록 없음</div>';
  } catch (error) { fail("timeline", error); }
}

function resetTimelineFilters() {
  ["timeline-type","timeline-author","timeline-delivery","timeline-from","timeline-to"].forEach(id => { const control = document.getElementById(id); if (control) control.value = ""; });
  loadTimeline();
}

async function loadAudit() {
  if (!selectedProjectId) return;
  try {
    const data = await get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/audit?limit=100`);
    renderAudit(data.items);
  } catch (error) { fail("audit-list", error); }
}

async function load() {
  const generation = ++loadGeneration;
  try {
    const projects = await get("/api/war-room/projects");
    if (generation !== loadGeneration) return;
    currentProjects = projects.items || [];
    const requestedProjectId = new URLSearchParams(window.location.search).get("project_id") || storedUiState().projectId;
    if (!selectedProjectId && requestedProjectId && currentProjects.some(row => row.id === requestedProjectId)) selectedProjectId = requestedProjectId;
    if (pinnedTaskId) {
      pinnedLookupError = null;
      try {
        const exact = await get(`/api/war-room/tasks/${encodeURIComponent(pinnedTaskId)}`);
        if (generation !== loadGeneration) return;
        selectedProjectId = exact.task.project_id;
        selectedTaskId = pinnedTaskId;
        quickTaskId = pinnedTaskId;
        if (!currentProjects.some(row => row.id === selectedProjectId)) {
          const detail = await get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`);
          currentProjects.unshift({...detail.project, participant_count:0, task_count:"-"});
        }
      } catch (error) {
        pinnedLookupError = error.message;
      }
    }
    if (!selectedProjectId && currentProjects[0]) selectedProjectId = currentProjects[0].id;
    if (selectedProjectId && !currentProjects.some(row => row.id === selectedProjectId)) selectedProjectId = currentProjects[0]?.id || null;
    if (generation !== loadGeneration) return;
    document.getElementById("projects").innerHTML = currentProjects.map(row => `<button class="project ${row.id === selectedProjectId ? "active" : ""}" onclick="selectProject('${esc(row.id)}')"><strong>${esc(row.name)}</strong><div>${statusChip(row.status)}</div><small>참여자 ${row.participant_count} · 작업 ${row.task_count ?? "-"}</small></button>`).join("") || '<div class="empty">프로젝트 없음</div>';
  } catch (error) { fail("projects", error); return; }
  if (!selectedProjectId) return;
  const projectId = selectedProjectId;
  try {
    const access = await get(`/api/war-room/projects/${encodeURIComponent(projectId)}/access`);
    if (generation !== loadGeneration || projectId !== selectedProjectId) return;
    applyAccess(access);
  }
  catch (error) { fail("audit-summary", error); return; }
  try {
    const base = `/api/war-room/projects/${encodeURIComponent(projectId)}`;
    const [detail, participants, operations, baseline, tasks, audit, deliveries, candidates, processBoard, readiness, documents] = await Promise.all([
      get(base), get(`${base}/participants`), get(`${base}/operations`),
      get(`${base}/manyfast-baseline`), get(`${base}/tasks`), get(`${base}/audit?limit=100`), get(`${base}/deliveries`), get(`${base}/agent-candidates`), get(`${base}/process-board`),
      get(`${base}/readiness`).catch(() => ({mode:"unavailable"})), get(`${base}/documents`),
    ]);
    if (generation !== loadGeneration || projectId !== selectedProjectId) return;
    currentProject = detail.project;
    currentParticipants = participants.items;
    currentAgentCatalog = candidates.items || [];
    currentTasks = tasks.items;
    currentDocuments = documents.items || [];
    currentDeliveries = deliveries.items || [];
    currentProcessBoard = processBoard;
    currentReadiness = readiness;
    selectedDocumentVersion = baseline.version;
    document.getElementById("project-edit-name").value = currentProject.name;
    document.getElementById("project-edit-status").value = currentProject.status;
    document.getElementById("baseline").textContent = `ManyFast ${baseline.version}`;
    const driftBanner = document.getElementById("manyfast-drift-banner");
    if (driftBanner) { driftBanner.hidden = !baseline.drift; driftBanner.textContent = baseline.drift ? `Manyfast 참고 버전 변경${baseline.drift_from ? `: ${baseline.drift_from} → ` : " → "}${baseline.version}. 기존 작업·원문·revision·승인은 그대로 보존됩니다.` : ""; }
    document.getElementById("task-document-version").value = baseline.version;
    if (selectedTaskId && !currentTasks.some(task => task.id === selectedTaskId)) selectedTaskId = null;
    if (pinnedTaskId && currentTasks.some(task => task.id === pinnedTaskId)) selectedTaskId = pinnedTaskId;
    renderProjectDetail(); renderDashboard(currentProjects, operations);
    renderTasks(); renderDocuments(); renderParticipants(); renderAgentControls(); renderAudit(audit.items); applyAccess();
    renderQuickDeliveries(currentDeliveries);
    document.getElementById("delivery-cards").innerHTML = currentDeliveries.map(row => `<div class="project"><strong>${esc(row.agent_id)} · ${statusChip(row.error_class === "system_error" ? "system_error" : row.status)}</strong><small>run ${esc(row.run_id || "-")} · source ${esc(row.message_id || "-")} · response ${esc(row.response_message_id || "-")}</small><small>retry ${row.retry_count || row.attempt_count || 0}/${row.max_attempts} · ${esc(row.error_code || "정상")}</small>${row.error_class === "system_error" && ["failed","timed_out"].includes(row.status) ? `<button data-permission="execute" onclick="retryDelivery('${esc(row.id)}')">수동 재전송</button>` : ""}</div>`).join("") || '<div class="empty">delivery 없음</div>';
    document.getElementById("stop-ack-delivery").innerHTML = '<option value="">현재 중지 cycle delivery 선택</option>' + currentDeliveries.filter(row => row.status === "stopped" && row.stop_cycle_at === deliveries.stop_requested_at).map(row => `<option value="${esc(row.id)}">${esc(row.agent_id)} · ${esc(row.id.slice(0,8))}</option>`).join("");
    renderTaskDetail(); renderReview(); renderProcessBoard(); applyAccess(); updateLocation();
    await loadTimeline();
  } catch (error) { fail("tasks", error); }
}

function showProjectForm() {
  document.getElementById("project-form").hidden = false;
  document.getElementById("project-name").focus();
  applyAccess();
}

function selectProject(id) {
  selectedProjectId = id;
  selectedTaskId = null; quickTaskId = null; pinnedTaskId = null;
  currentProject = null; currentParticipants = []; currentTasks = []; currentDocuments = []; currentReadiness = null;
  updateLocation({push:true});
  load();
}

function syncParticipantFlags() {
  const flags = {project_manager:[1,1,1,1],developer:[1,1,0,0],qa:[1,1,0,0],observer:[1,0,0,0]}[document.getElementById("participant-role").value];
  ["read","comment","approve","execute"].forEach((name,index) => { document.getElementById(`participant-${name}`).checked = Boolean(flags[index]); });
}

function editParticipant(principal) {
  const row = currentParticipants.find(item => item.principal_id === principal);
  if (!row) return;
  const principalSelect = document.getElementById("participant-principal");
  principalSelect.dataset.editingPrincipal = row.principal_id;
  principalSelect.innerHTML = `<option value="${esc(row.principal_id)}">${esc(row.principal_id)}</option>`;
  principalSelect.value = row.principal_id;
  principalSelect.disabled = true;
  document.getElementById("participant-role").value = row.role;
  ["read","comment","approve","execute"].forEach(name => { document.getElementById(`participant-${name}`).checked = Boolean(row[`can_${name}`]); });
  document.getElementById("project-management-result").textContent = `${row.principal_id} 수정 모드`;
}

async function setParticipantActive(principal, active) {
  const out = document.getElementById("project-management-result");
  try {
    const url = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/participants/${encodeURIComponent(principal)}`;
    await post(url, {active}, "PATCH"); out.textContent = `${principal} ${active ? "재활성" : "비활성"} 완료`; await load();
  } catch (error) { out.textContent = error.message; }
}

async function archiveProject() {
  if (!selectedProjectId || !confirm("이 프로젝트를 보관하시겠습니까?")) return;
  const out = document.getElementById("project-management-result");
  try { await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/archive`, {}); out.textContent = "프로젝트를 보관했습니다"; await load(); }
  catch (error) { out.textContent = error.message; }
}

function selectApprovalTask(id) {
  document.getElementById("approval-task").value = id;
  openTask(id, "task");
}

async function approveSelectedTask() {
  const out = document.getElementById("approval-result");
  try { await withMutation(`approve:${document.getElementById("approval-task").value}`, "approval-result", async () => {
    const task = await requireFreshTask(document.getElementById("approval-task").value, ["awaiting_approval"]);
    const expires_at = Math.floor(Date.now() / 1000) + Number(document.getElementById("approval-expiry").value);
    const result = await post(`/api/war-room/tasks/${task.id}/approvals`, {...mutationContract(task), decision:"approved",expires_at});
    out.textContent = `승인 완료 · ${result.approval_id.slice(0,8)}`; await load();
  }); } catch (_) { /* withMutation reports the exact failure */ }
}

async function rejectSelectedTask() {
  const out = document.getElementById("approval-result");
  try { await withMutation(`reject:${document.getElementById("approval-task").value}`, "approval-result", async () => { const task = await requireFreshTask(document.getElementById("approval-task").value, ["awaiting_approval"]); await post(`/api/war-room/tasks/${task.id}/approvals`, {...mutationContract(task), decision:"rejected"}); out.textContent = "거절 완료"; await load(); }); }
  catch (_) { /* reported */ }
}

async function deliverTask(id) {
  const out = document.getElementById("approval-result");
  try { await withMutation(`deliver:${id}`, "approval-result", async () => {
    const task = await requireFreshTask(id, ["running"]);
    if (!task || !task.source_message_id) throw new Error("연결된 지시가 없습니다");
    if (currentDeliveries.some(row => row.task_id === task.id && Number(row.task_revision || 1) === Number(task.revision || 1))) throw new Error("현재 revision에 이미 생성된 delivery가 있어 중복 전달하지 않습니다");
    const url = `/api/war-room/messages/${task.source_message_id}/deliveries`;
    const payload = {agent_ids: task.agent_ids || [task.assignee_agent_id], task_id: task.id};
    const result = await post(url, payload);
    out.textContent = `${payload.agent_ids.join(", ")} 전달 ${result.status}`; await load();
  }); } catch (_) { /* reported */ }
}

async function runSelectedTask() {
  const out = document.getElementById("approval-result");
  try { await withMutation(`run:${document.getElementById("approval-task").value}`, "approval-result", async () => {
    const task = await requireFreshTask(document.getElementById("approval-task").value, ["approved","running"]);
    if (task.status === "approved") await post(`/api/war-room/tasks/${task.id}/transition`, {...mutationContract(task), status:"running"});
    else if (task.status !== "running") throw new Error("승인된 작업만 실행할 수 있습니다");
    await load();
    const refreshed = taskById(task.id);
    if (refreshed?.source_message_id) await deliverTask(refreshed.id);
    else out.textContent = "실행 시작됨 · 연결된 지시는 없음";
  }); } catch (_) { /* reported */ }
}

async function saveResult() {
  const out = document.getElementById("qa-result");
  try {
    const task = await requireFreshTask(document.getElementById("qa-task").value, ["running","qa"]);
    const body = document.getElementById("result-body").value.trim();
    if (!body) throw new Error("작업 결과를 입력하세요");
    const url = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/messages`;
    await post(url, {message_type:"result",body:`[task ${task.id}] ${body}`,source_message_id:task.source_message_id || null});
    document.getElementById("result-body").value = ""; out.textContent = "결과를 타임라인에 저장했습니다"; await load(); await renderReview();
  } catch (error) { out.textContent = error.message; }
}

async function addEvidence() {
  const out = document.getElementById("qa-result");
  try {
    const task = await requireFreshTask(document.getElementById("qa-task").value, ["qa"]);
    const body = {uri:document.getElementById("evidence-uri").value.trim(),summary:document.getElementById("evidence-summary").value.trim(),evidence_type:document.getElementById("evidence-type").value};
    const result = await post(`/api/war-room/tasks/${task.id}/evidence`, body);
    out.textContent = `증거 추가 · ${result.evidence_id.slice(0,8)}`; await load(); await renderReview();
  } catch (error) { out.textContent = error.message; }
}

async function submitQaVerdict() {
  const out = document.getElementById("qa-result");
  try {
    const task = await requireFreshTask(document.getElementById("qa-task").value, ["qa"]);
    const body = {verdict:document.getElementById("qa-verdict").value,evidence_profile:"required:test,artifact",qa_principal:projectAccess.principal_id,source:"agent_result"};
    const result = await post(`/api/war-room/tasks/${task.id}/qa-verdict`, body);
    out.textContent = `QA ${result.verdict} 저장 완료`; await load();
  } catch (error) { out.textContent = error.message; }
}

async function stopProject() {
  if (!selectedProjectId || !confirm("현재 실행을 중지하시겠습니까?")) return;
  const out = document.getElementById("stop-result");
  try {
    const url = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/stop`;
    const result = await guardedPost(url, {}, "project_stop", selectedProjectId);
    out.textContent = `중지 요청 ${result.status}`;
    await load();
  }
  catch (error) { out.textContent = error.message; }
}

async function stopTask(id) {
  const out = document.getElementById("stop-result");
  try { await withMutation(`stop:${id}`, "stop-result", async () => {
    const task = await requireFreshTask(id, ["approved","running","qa"]);
    const result = await guardedPost(`/api/war-room/tasks/${encodeURIComponent(task.id)}/stop`, {}, "task_stop", task.id);
    out.textContent = result.confirmed
      ? `작업 중지 확인 · ${task.id} · delivery ${(result.delivery_ids || []).length}건`
      : `작업 중지 요청 · ${task.id} · ${result.status}`;
    await load();
  }); } catch (_) { /* reported */ }
}

async function stopSelectedTask() {
  const id = document.getElementById("approval-task").value;
  if (!id) { document.getElementById("stop-result").textContent = "중지할 작업을 선택하세요"; return; }
  await stopTask(id);
}

async function prepareTaskForReapproval(id) {
  const out = document.getElementById("approval-result");
  try { await withMutation(`reapproval:${id}`, "approval-result", async () => {
    const task = await requireFreshTask(id, ["rework_required","stopped","stop_unconfirmed"]);
    const result = await post(`/api/war-room/tasks/${encodeURIComponent(task.id)}/transition`, {
      ...mutationContract(task), status:"awaiting_approval",
      deadline_at:Math.floor(Date.now()/1000)+1800,
    });
    out.textContent = `재승인 준비 완료 · ${result.status || "awaiting_approval"} · 새 승인이 필요합니다`;
    await load();
  }); } catch (_) { /* reported */ }
}

async function prepareSelectedTaskForReapproval() {
  const id = document.getElementById("approval-task").value;
  if (!id) { document.getElementById("approval-result").textContent = "재승인할 작업을 선택하세요"; return; }
  await prepareTaskForReapproval(id);
}

async function acknowledgeStop() {
  const out = document.getElementById("stop-result");
  try { const delivery_id=document.getElementById("stop-ack-delivery").value; if(!delivery_id) throw new Error("중지된 delivery를 선택하세요"); const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/stop-ack`, {delivery_id}); out.textContent = `중지 확인 ${result.status}`; await load(); }
  catch (error) { out.textContent = error.message; }
}

async function resumeProject() {
  const out = document.getElementById("stop-result");
  try { const result = await guardedPost(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/resume`, {}, "project_resume", selectedProjectId); out.textContent = `재개 ${result.status}`; await load(); }
  catch (error) { out.textContent = `재개 실패 · ${error.message}`; }
}

async function changeTask(id, status) {
  try { const task = await requireFreshTask(id); await post(`/api/war-room/tasks/${id}/transition`, {...mutationContract(task), status}); await load(); }
  catch (error) { alert(error.message); }
}

async function representativeComplete(id) {
  try { await guardedPost(`/api/war-room/tasks/${id}/representative-completion`, {decision:"approved"}, "representative_completion", id); await load(); }
  catch (error) { alert(error.message); }
}

async function refreshTasks() { await load(); }

document.getElementById("task-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("task-result");
  try {
    const agent_ids=[...document.querySelectorAll('#task-agent-targets input:checked')].map(input=>input.value); const assignee_agent_id=document.getElementById("task-assignee").value; if(!agent_ids.includes(assignee_agent_id)) agent_ids.unshift(assignee_agent_id);
    const instruction = document.getElementById("task-scope").value;
    const body = {instruction,scope:instruction,assignee_agent_id,reviewer_agent_id:document.getElementById("task-reviewer").value,agent_ids,execution_mode:"FAST_GATEWAY",call_limit:Number(document.getElementById("task-call-limit").value),turn_limit:Number(document.getElementById("task-turn-limit").value),deadline_at:Math.floor(Date.now()/1000)+Number(document.getElementById("task-deadline").value),document_version:document.getElementById("task-document-version").value};
    const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/prepare`, body);
    out.textContent = `승인 요청 준비됨 ${result.task_id.slice(0,8)} · 아직 Agent를 호출하지 않았습니다`; document.getElementById("task-scope").value = ""; selectedTaskId = result.task_id; await load();
  } catch (error) { out.textContent = error.message; }
});

document.getElementById("quick-execution-mode").addEventListener("change", event => {
  document.getElementById("command-center-panel").hidden = event.target.value !== "command_center";
});

document.getElementById("command-center-dispatch").addEventListener("click", async () => {
  const button = document.getElementById("command-center-dispatch");
  const out = document.getElementById("command-center-status");
  try {
    const task = window.commandCenterTask;
    if (!task) throw new Error("Command Center task가 없습니다");
    button.disabled = true; out.textContent = "명시적 dispatch 중…";
    const result = await post(`/api/war-room/command-center/tasks/${encodeURIComponent(task.task_id)}/dispatch`, {});
    out.textContent = `Dispatch 완료 · ${result.run_status || result.status || "accepted"}`;
    const [status, summary] = await Promise.all([
      get(`/api/war-room/command-center/${encodeURIComponent(task.war_project_id)}/tasks/${encodeURIComponent(task.war_task_id)}`),
      get(`/api/war-room/command-center/${encodeURIComponent(task.war_project_id)}/tasks/${encodeURIComponent(task.war_task_id)}/summary`),
    ]);
    out.textContent += ` · 상태 ${summary.overall || status.tasks?.[0]?.run_status || "확인됨"}`;
  } catch (error) { button.disabled = false; out.textContent = `Command Center 실패 · ${error.message}`; }
});

document.getElementById("quick-task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const out = document.getElementById("quick-result");
  try {
    if (pinnedTaskId !== null) {
      const task = taskById(pinnedTaskId);
      if (!task) throw new Error(`고정 task를 찾을 수 없습니다: ${pinnedTaskId}`);
      quickTaskId = task.id; selectedTaskId = task.id; openTask(task.id);
      out.textContent = `기존 task ${task.id}를 선택했습니다. 새 task는 생성하지 않았습니다.`;
      return;
    }
    const instruction = document.getElementById("quick-instruction").value.trim();
    const assignee_agent_id = document.getElementById("quick-assignee").value;
    const reviewer_agent_id = document.getElementById("quick-reviewer").value;
    // Quick start is the one-worker path: the selected assignee is the only
    // execution target; reviewer remains an independent QA principal.
    const agent_ids = assignee_agent_id ? [assignee_agent_id] : [];
    if (!instruction) throw new Error("작업 내용을 입력하세요");
    if (!agent_ids.length) throw new Error("담당 에이전트를 한 명 이상 선택하세요");
    out.textContent = "작업을 준비하고 있습니다…";
    if (document.getElementById("quick-execution-mode").value === "command_center") {
      const result = await post("/api/war-room/command-center/submit", {
        war_project_id: selectedProjectId,
        war_task_id: `ui-${crypto.randomUUID()}`,
        scope: instruction,
        requested_agents: agent_ids,
        assignee_agent_id: agent_ids[0],
        workspace_id: "command-center",
      });
      const first = result.command_tasks?.[0];
      if (!first) throw new Error("Command Center가 실행 후보를 반환하지 않았습니다");
      window.commandCenterTask = { ...first, war_project_id: result.war_project_id, war_task_id: result.war_task_id };
      const candidates = await get(`/api/war-room/command-center/${encodeURIComponent(result.war_project_id)}/tasks/${encodeURIComponent(result.war_task_id)}/candidates`);
      document.getElementById("command-center-panel").hidden = false;
      document.getElementById("command-center-dispatch").hidden = !candidates.candidates?.length;
      document.getElementById("command-center-status").textContent = candidates.candidates?.length ? `Accepted · READY 후보 ${candidates.candidates.length}개` : "Accepted · READY 후보 없음";
      out.textContent = "Command Center 작업이 접수되었습니다. 후보를 확인한 뒤 명시적으로 Dispatch하세요.";
      return;
    }
    const base = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`;
    const task = await post(`${base}/prepare`, {
      instruction,
      scope: instruction,
      assignee_agent_id,
      reviewer_agent_id,
      execution_mode: "FAST_GATEWAY",
      agent_ids,
      deadline_at: Math.floor(Date.now() / 1000) + 1800,
      document_version: selectedDocumentVersion,
    });
    quickTaskId = task.task_id;
    document.getElementById("quick-instruction").value = "";
    await load();
    out.textContent = `승인 요청을 준비했습니다. Agent 호출 0건 · 대표 승인 후 한 번 실행됩니다.`;
  } catch (error) { out.textContent = `준비 실패: ${error.message}`; }
});

function ensureExecutionModeControls() {
  [["quick-task-form", "quick-execution-mode", "빠른 시작 실행 모드"], ["task-form", "task-execution-mode", "작업 실행 모드"]].forEach(([formId, id, labelText]) => {
    const form = document.getElementById(formId);
    if (!form || document.getElementById(id)) return;
    const label = document.createElement("label"); label.textContent = `${labelText} `;
    const select = document.createElement("select"); select.id = id;
    select.innerHTML = '<option value="FAST_GATEWAY">Fast Gateway · 공통 Core/Broker/Harness</option>';
    select.disabled = true;
    label.appendChild(select); form.insertBefore(label, form.firstChild);
  });
}

async function quickApproveAndRun() {
  const out = document.getElementById("quick-result");
  const id = pinnedTaskId !== null ? pinnedTaskId : quickTaskId;
  try { await withMutation(`quick-run:${id}`, "quick-result", async () => {
    const task = await requireFreshTask(id, ["awaiting_approval","running"]);
    out.textContent = "승인하고 에이전트에게 전달하고 있습니다…";
    try {
      if (task.status === "awaiting_approval") await guardedPost(`/api/war-room/tasks/${task.id}/approve-execute`, {...mutationContract(task), expires_at:Math.floor(Date.now()/1000)+1800}, "task_approve_execute", task.id);
      else if (task.status === "running") return;
    }
    catch (error) {
      if (!demoMode || !error.message.includes("active agent call already exists")) throw error;
      out.textContent = "이전 시연 응답을 정리한 뒤 자동으로 다시 전달합니다…";
      await post('/api/war-room/demo/process', {});
    }
    await load();
    out.textContent = "실행을 시작했습니다. 아래 결과 카드에서 진행 상태를 확인하세요.";
    if (demoMode) document.getElementById("quick-demo-process").hidden = false;
  }); } catch (error) { out.textContent = `실행 실패: ${error.message}`; }
}

function quickOpenQa() {
  const task = taskById(pinnedTaskId !== null ? pinnedTaskId : quickTaskId);
  if (!task || task.status !== "qa") return;
  const area = document.getElementById("advanced-area");
  area.hidden = false;
  document.getElementById("qa-task").value = task.id;
  navigateScreen("review");
  document.getElementById("qa-task").focus();
  document.getElementById("task-review").scrollIntoView({behavior:"smooth",block:"center"});
  document.getElementById("results-qa").scrollIntoView({behavior:"smooth",block:"center"});
}

async function quickCompleteTask() {
  const out = document.getElementById("quick-result");
  try {
    const task = await requireFreshTask(pinnedTaskId !== null ? pinnedTaskId : quickTaskId, ["qa"]);
    if (task.latest_qa_verdict?.verdict !== "PASS" || Number(task.evidence_count || 0) < 1) throw new Error("QA PASS와 필수 증거가 필요합니다");
    await guardedPost(`/api/war-room/tasks/${task.id}/representative-completion`, {decision:"approved"}, "representative_completion", task.id);
    await load(); out.textContent = "최종 완료 처리했습니다.";
  } catch (error) { out.textContent = `완료 실패: ${error.message}`; }
}

async function quickProcessResults() {
  const out = document.getElementById("quick-result");
  try { out.textContent = "시연 응답을 처리하고 있습니다…"; await processDemoQueue(); out.textContent = "응답 처리가 끝났습니다. 결과 상태를 확인하세요."; }
  catch (error) { out.textContent = `응답 처리 실패: ${error.message}`; }
}

document.getElementById("document-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const out = document.getElementById("document-result");
  try {
    const documentId = document.getElementById("document-id").value.trim();
    const expectedRaw = document.getElementById("document-expected-version").value;
    const taskId = document.getElementById("document-task").value;
    const body = {
      title: document.getElementById("document-title").value,
      category: document.getElementById("document-category").value,
      uri: document.getElementById("document-uri").value,
      summary: document.getElementById("document-summary").value,
      relation: document.getElementById("document-relation").value,
    };
    if (documentId) body.document_id = documentId;
    if (expectedRaw !== "") body.expected_version = Number(expectedRaw);
    if (taskId) body.task_id = taskId;
    const result = await post(
      `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/documents/register`, body
    );
    out.textContent = `등록 완료 · v${result.version} · ${result.changed ? "새 버전" : "동일 내용"}`;
    await load();
  } catch (error) { out.textContent = error.message; }
});

document.getElementById("project-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("project-result");
  try { const result = await post("/api/war-room/projects", {name:document.getElementById("project-name").value}); selectedProjectId = result.project_id; out.textContent = `생성됨 ${result.project_id.slice(0,8)}`; await load(); }
  catch (error) { out.textContent = error.message; }
});

document.getElementById("project-edit-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("project-management-result");
  try { const body = {name:document.getElementById("project-edit-name").value,status:document.getElementById("project-edit-status").value}; await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`, body, "PATCH"); out.textContent = "프로젝트 수정 완료"; await load(); }
  catch (error) { out.textContent = error.message; }
});

document.getElementById("participant-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("project-management-result");
  try {
    const principal_id = document.getElementById("participant-principal").value;
    const body = {
      principal_id,
      role: document.getElementById("participant-role").value,
      can_read: document.getElementById("participant-read").checked,
      can_comment: document.getElementById("participant-comment").checked,
      can_approve: document.getElementById("participant-approve").checked,
      can_execute: document.getElementById("participant-execute").checked,
    };
    const exists = currentParticipants.some(row => row.principal_id === principal_id);
    const suffix = exists ? `/participants/${encodeURIComponent(principal_id)}` : "/participants";
    const url = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}${suffix}`;
    await post(url, body, exists ? "PATCH" : "POST");
    out.textContent = `${principal_id} 저장 완료`;
    const principalSelect = document.getElementById("participant-principal");
    delete principalSelect.dataset.editingPrincipal; principalSelect.disabled = false;
    await load();
  } catch (error) { out.textContent = error.message; }
});

document.getElementById("message-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("message-result");
  try {
    const base = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`;
    const taskId = document.getElementById("message-task").value;
    const selectedAgent = document.getElementById("message-agent").value;
    const messageType = document.getElementById("message-type").value;
    if (messageType === "opinion") throw new Error("Agent 의견 실제 요청 기능은 미구현입니다. 기록만 생성하지 않습니다.");
    const task = taskById(taskId);
    const bodyText = document.getElementById("message-body").value;
    const result = await post(`${base}/instructions`, {task_id:taskId, body:bodyText});
    out.textContent = `연결 지시 기록 · ${(result.id || result.message_id).slice(0,8)}`;
    document.getElementById("message-body").value = "";
    await load(); if (taskId) document.getElementById("approval-task").value = taskId;
  } catch (error) { out.textContent = `지시·작업 생성 실패: ${error.message}`; }
});

async function bindDemoSession(){const agent=document.getElementById("demo-session-agent").value; await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/participants/${agent}/test-session`,{session_key:document.getElementById("demo-session-key").value,session_id:document.getElementById("demo-session-id").value},"PUT"); await load();}
function renderJevAdvisory(taskId, row) {
  const box = document.getElementById(`jev-advisory-${taskId}`); if (!box) return;
  const controls = row.status === "AVAILABLE" ? `<div class="filterbar"><button onclick="decideJev('${esc(taskId)}','${esc(row.advisory_id)}','ACCEPT')">ACCEPT</button><button onclick="decideJev('${esc(taskId)}','${esc(row.advisory_id)}','INSUFFICIENT')">INSUFFICIENT</button><button onclick="decideJev('${esc(taskId)}','${esc(row.advisory_id)}','OVERRIDE')">OVERRIDE</button></div>` : '';
  box.hidden = false; box.innerHTML = `<div class="notice">Advisory only · assignment/dispatch/lifecycle 변경 없음</div><div>Domain: <strong>${esc(row.domain_choice || "unavailable")}</strong> · Agent: <strong>${esc(row.agent_choice || "unavailable")}</strong></div><div class="muted">probabilities ${esc(JSON.stringify(row.domain_probabilities || {}))} / ${esc(JSON.stringify(row.agent_probabilities || {}))}</div><div class="muted">confidence ${esc(row.domain_confidence ?? "-")} / ${esc(row.agent_confidence ?? "-")} · latency ${esc(row.latency_ms)}ms · ${row.stale ? "STALE" : "fresh"}</div>${row.error ? `<div class="error">${esc(row.error.code)}: ${esc(row.error.detail)}</div>` : ''}${controls}`;
}
function renderTaskDetailJev(row) {
  const box = document.getElementById("jev-task-detail-content"); if (!box) return;
  if (!row) { box.innerHTML = '<span class="muted">최신 advisory 없음 · 먼저 advisory를 요청하세요.</span>'; return; }
  const controls = row.status === "AVAILABLE" ? `<div class="filterbar"><button onclick="decideJev('${esc(row.task_id)}','${esc(row.advisory_id)}','ACCEPT')">ACCEPT</button><button onclick="decideJev('${esc(row.task_id)}','${esc(row.advisory_id)}','INSUFFICIENT')">INSUFFICIENT</button><button onclick="decideJev('${esc(row.task_id)}','${esc(row.advisory_id)}','OVERRIDE')">OVERRIDE</button></div>` : '';
  box.innerHTML = `<div class="notice">${row.status} · advisory-only · lifecycle/assignment/delivery/dispatch unchanged</div><div>Domain: <strong>${esc(row.domain_choice || "-")}</strong> · Agent: <strong>${esc(row.agent_choice || "-")}</strong></div><div class="muted">probabilities ${esc(JSON.stringify(row.domain_probabilities || {}))} / ${esc(JSON.stringify(row.agent_probabilities || {}))}</div><div class="muted">confidence ${esc(row.domain_confidence ?? "-")} / ${esc(row.agent_confidence ?? "-")} · latency ${esc(row.latency_ms ?? "-")}ms · ${row.stale ? "STALE" : "fresh"}</div>${row.latest_decision ? `<div>Latest decision: <strong>${esc(row.latest_decision.decision)}</strong> · actor ${esc(row.latest_decision.actor_id)}</div>` : ''}${row.error ? `<div class="error">${esc(row.error.code)}: ${esc(row.error.detail)}</div>` : ''}${controls}`;
}
async function loadTaskDetailJev(taskId) {
  const box = document.getElementById("jev-task-detail-content"); if (!box || !taskId) return;
  box.innerHTML = '<span class="muted">최신 advisory 조회 중…</span>';
  try { renderTaskDetailJev(await get(`/api/war-room/tasks/${encodeURIComponent(taskId)}/jev-advisory`)); }
  catch (_) { renderTaskDetailJev(null); }
}
function renderTaskResultVerifier(row) {
  const box = document.getElementById("jev-result-verifier-content"); if (!box) return;
  if (!row) { box.innerHTML = '<span class="muted">Result Verifier advisory 없음.</span><button onclick="requestTaskResultVerifier()">advisory 요청</button>'; return; }
  box.innerHTML = `<div class="notice">${row.stale ? "STALE" : "fresh"} · advisory-only · lifecycle/assignment/delivery/dispatch/QA unchanged</div><div class="muted">run ${esc(row.run_id || "-")} · revision ${esc(row.task_revision)} · ${esc(row.state_digest)}</div><ul>${(row.signals || []).map(signal => `<li><strong>${esc(signal.signal)}</strong>: ${esc(signal.choice || signal.status)} · p=${esc(JSON.stringify(signal.probabilities || {}))} · ${esc(signal.latency_ms)}ms${signal.error_code ? ` · ${esc(signal.error_code)}` : ""}</li>`).join("")}</ul><button onclick="requestTaskResultVerifier()">새 advisory 요청</button>`;
}
async function loadTaskResultVerifier(taskId) {
  const box = document.getElementById("jev-result-verifier-content"); if (!box || !taskId) return;
  try { renderTaskResultVerifier(await get(`/api/war-room/tasks/${encodeURIComponent(taskId)}/jev-result-verifier`)); } catch (_) { renderTaskResultVerifier(null); }
}
async function requestTaskResultVerifier() {
  const task = selectedCurrentTask(); if (!task) return;
  const box = document.getElementById("jev-result-verifier-content"); box.innerHTML = '<span class="muted">Result Verifier advisory 조회 중…</span>';
  try { renderTaskResultVerifier(await post(`/api/war-room/tasks/${encodeURIComponent(task.id)}/jev-result-verifier`, {})); }
  catch (error) { box.innerHTML = `<div class="error">ADVISORY_UNAVAILABLE · ${esc(error.message)} · 수동 lifecycle/QA 흐름은 계속 사용 가능합니다.</div>`; }
}
async function requestJevAdvisory(taskId) {
  const box = document.getElementById(`jev-advisory-${taskId}`); if (!box) return;
  box.hidden = false; box.innerHTML = '<div class="muted">JEV advisory 조회 중…</div>';
  try {
    const row = await post(`/api/war-room/tasks/${encodeURIComponent(taskId)}/jev-advisory`, {});
    renderJevAdvisory(taskId, row);
  } catch (error) { box.innerHTML = `<div class="error">Advisory unavailable: ${esc(error.message)} · manual assignment remains usable</div>`; }
}
async function decideJev(taskId, advisoryId, decision) {
  const payload = {decision};
  if (decision === "OVERRIDE") { payload.override_agent_id = window.prompt("등록·활성 후보 Agent ID"); payload.override_reason = window.prompt("Override reason"); }
  try { await post(`/api/war-room/tasks/${encodeURIComponent(taskId)}/jev-advisory/${encodeURIComponent(advisoryId)}/decision`, payload); const latest = await get(`/api/war-room/tasks/${encodeURIComponent(taskId)}/jev-advisory`); renderJevAdvisory(taskId, latest); renderTaskDetailJev(latest); }
  catch (error) { window.alert(`JEV decision failed: ${error.message}`); }
}
async function processDemoQueue(){await post('/api/war-room/demo/process',{}); await load();}
async function retryDemoDelivery(id){await post(`/api/war-room/deliveries/${id}/retry`,{}); await load();}
window.addEventListener("popstate", () => {
  const params = new URLSearchParams(window.location.search);
  selectedProjectId = params.get("project_id");
  selectedTaskId = params.get("task_id");
  quickTaskId = selectedTaskId;
  pinnedTaskId = selectedTaskId;
  currentScreen = params.get("screen") || (selectedTaskId ? "task" : "dashboard");
  navigateScreen(currentScreen, {push:false});
  load();
});

document.getElementById("approval-task")?.addEventListener("change", event => {
  if (event.target.value) openTask(event.target.value, "task");
});
document.getElementById("qa-task")?.addEventListener("change", event => {
  if (event.target.value) openTask(event.target.value, "task");
});

const restoredUi = storedUiState();
if (!selectedProjectId) selectedProjectId = new URLSearchParams(window.location.search).get("project_id") || restoredUi.projectId || null;
if (!selectedTaskId) selectedTaskId = pinnedTaskId || restoredUi.taskId || null;
currentScreen = new URLSearchParams(window.location.search).get("screen") || restoredUi.screen || (selectedTaskId ? "task" : "dashboard");
if (document.getElementById("task-status-filter")) document.getElementById("task-status-filter").value = restoredUi.statusFilter || "";
if (document.getElementById("task-search-filter")) document.getElementById("task-search-filter").value = restoredUi.searchFilter || "";
navigateScreen(currentScreen, {push:false});
ensureExecutionModeControls();
get('/api/war-room/demo-mode').then(()=>{demoMode=true;document.getElementById('demo-controls').hidden=false;}).catch(()=>{}).finally(load);
setInterval(() => { if (!document.querySelector("form:focus-within") && mutationInFlight.size === 0) load(); }, 15000);
