let selectedProjectId = null;
let selectedDocumentVersion = null;
let projectAccess = {permissions: []};
let currentProject = null;
let currentParticipants = [];
let currentTasks = [];
let demoMode = false;
let quickTaskId = null;

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

function fail(id, error) {
  document.getElementById(id).innerHTML = `<div class="empty error">조회 실패: ${esc(error.message)}</div>`;
}

function applyAccess(access = projectAccess) {
  projectAccess = access;
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
}

function taskById(id) {
  return currentTasks.find(task => task.id === id);
}

function selectedTask(selectId) {
  const task = taskById(document.getElementById(selectId).value);
  if (!task) throw new Error("작업을 선택하세요");
  return task;
}

function renderTasks() {
  if (!quickTaskId) {
    quickTaskId = currentTasks.find(task => ["awaiting_approval","approved","running","qa"].includes(task.status) && task.source_message_id)?.id
      || currentTasks.find(task => task.status === "completed" && task.source_message_id)?.id || null;
  }
  document.getElementById("tasks").innerHTML = currentTasks.map(task => {
    const buttons = [];
    if (task.status === "draft") buttons.push(`<button data-permission="manage" onclick="changeTask('${task.id}','awaiting_approval')">승인 요청</button>`);
    if (task.status === "awaiting_approval") {
      buttons.push(`<button data-permission="approve" onclick="selectApprovalTask('${task.id}')">승인 화면에 선택</button>`);
    }
    if (task.status === "approved") {
      buttons.push(`<button data-permission="execute" onclick="selectApprovalTask('${task.id}')">실행 준비</button>`);
    }
    if (task.status === "running" && task.source_message_id) {
      buttons.push(`<button data-permission="execute" onclick="deliverTask('${task.id}')">지시 전달</button>`);
    }
    if (task.status === "running") buttons.push(`<button data-permission="execute" onclick="changeTask('${task.id}','qa')">QA 전환</button>`);
    if (task.status === "qa") buttons.push(`<button data-permission="manage" onclick="representativeComplete('${task.id}')">대표 최종 완료</button>`);
    if (["running","qa"].includes(task.status)) buttons.push(`<button data-permission="execute" onclick="changeTask('${task.id}','stopped')">작업 중지</button>`);
    return `<div class="project"><strong>${esc(task.scope)}</strong><div class="muted">${esc(task.status)} · ${esc(task.assignee_agent_id)}</div><small>호출 ${task.call_limit} · 턴 ${task.turn_limit} · 문서 ${esc(task.document_version)}</small><div class="filterbar">${buttons.join("")}</div></div>`;
  }).join("") || '<div class="empty">작업 없음</div>';
  const options = currentTasks.map(task => `<option value="${esc(task.id)}">${esc(task.status)} · ${esc(task.scope.slice(0, 42))}</option>`);
  document.getElementById("message-task").innerHTML = '<option value="">작업 선택</option>' + options.filter(option => !option.includes("completed") && !option.includes("stopped")).join("");
  document.getElementById("approval-task").innerHTML = '<option value="">작업 선택</option>' + options.join("");
  document.getElementById("qa-task").innerHTML = '<option value="">QA 작업 선택</option>' + options.join("");
  renderQuickProgress();
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
    approve.disabled = true;
    approve.hidden = true;
  }
  const qaButton = document.getElementById("quick-open-qa");
  if (qaButton) qaButton.hidden = status !== "qa";
  const completeButton = document.getElementById("quick-complete");
  if (completeButton) completeButton.hidden = status !== "qa";
}

function deliveryLabel(status) {
  return ({queued:"전달 대기",received:"작업 중",responded:"응답 완료",failed:"실패",timed_out:"시간 초과",stopped:"중지"})[status] || status;
}

function renderQuickDeliveries(items) {
  const relevant = quickTaskId ? items.filter(row => row.task_id === quickTaskId) : [];
  document.getElementById("quick-delivery-cards").innerHTML = relevant.map(row =>
    `<div class="project"><strong>${esc(row.agent_id)}</strong><div class="${row.status === "responded" ? "status" : "muted"}">${esc(row.error_code === "agent_busy_queued" ? "다른 작업 종료 후 순서대로 실행" : deliveryLabel(row.status))}</div>${row.response_body ? `<p>${esc(row.response_body)}</p>` : ""}${row.error_code && row.error_code !== "agent_busy_queued" ? `<small class="error">${esc(row.error_code)}</small>` : ""}<small>${row.error_code === "agent_busy_queued" ? "대기 순서가 유지됩니다. 다시 누르지 마세요." : row.error_code ? "다음 행동: 오류를 수정한 뒤 재작업" : row.status === "responded" ? "다음 행동: 결과와 QA 판정을 비교" : "다음 행동: 처리 완료 대기"}</small></div>`
  ).join("") || '<div class="empty">실행 후 에이전트별 결과가 여기에 표시됩니다.</div>';
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

function renderAudit(items) {
  document.getElementById("audit-list").innerHTML = items.map(row => `<div class="project"><strong>${esc(row.event_type)}</strong><div>${esc(row.actor_id)} → ${esc(row.target_type)} ${esc(row.target_id)}</div><small>${new Date(row.created_at * 1000).toLocaleString()} · ${esc(row.correlation_id)}</small></div>`).join("") || '<div class="empty">감사 기록 없음</div>';
}

async function loadTimeline() {
  if (!selectedProjectId) return;
  const params = new URLSearchParams();
  const filters = {message_type:"timeline-type", author_id:"timeline-author", delivery_status:"timeline-delivery"};
  Object.entries(filters).forEach(([key,id]) => {
    const value = document.getElementById(id).value;
    if (value) params.set(key, value);
  });
  [["from_ts","timeline-from"],["to_ts","timeline-to"]].forEach(([key,id]) => {
    const value = document.getElementById(id).value;
    if (value) params.set(key, String(Math.floor(new Date(value).getTime() / 1000)));
  });
  try {
    const data = await get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/timeline?${params}`);
    document.getElementById("timeline").innerHTML = data.items.map(row => {
      const statuses = row.delivery_statuses ? ` · ${esc(row.delivery_statuses)}` : "";
      const time = new Date(row.created_at * 1000).toLocaleString();
      return `<article class="event"><div class="event-type">${esc(row.message_type)}</div><div><strong>${esc(row.author_id)}</strong><p>${esc(row.body)}</p><small>${time}${statuses}</small></div></article>`;
    }).join("") || '<div class="empty">기록 없음</div>';
  } catch (error) { fail("timeline", error); }
}

function resetTimelineFilters() {
  ["timeline-type","timeline-author","timeline-delivery","timeline-from","timeline-to"].forEach(id => { document.getElementById(id).value = ""; });
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
  try {
    const projects = await get("/api/war-room/projects");
    if (!selectedProjectId && projects.items[0]) selectedProjectId = projects.items[0].id;
    if (selectedProjectId && !projects.items.some(row => row.id === selectedProjectId)) selectedProjectId = projects.items[0]?.id || null;
    document.getElementById("projects").innerHTML = projects.items.map(row => `<button class="project ${row.id === selectedProjectId ? "active" : ""}" onclick="selectProject('${esc(row.id)}')"><strong>${esc(row.name)}</strong><div class="muted">${esc(row.status)} · 참여자 ${row.participant_count}</div></button>`).join("") || '<div class="empty">프로젝트 없음</div>';
  } catch (error) { fail("projects", error); return; }
  if (!selectedProjectId) return;
  try { applyAccess(await get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/access`)); }
  catch (error) { fail("audit-summary", error); return; }
  try {
    const base = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`;
    const [detail, participants, operations, baseline, tasks, audit, deliveries] = await Promise.all([
      get(base), get(`${base}/participants`), get(`${base}/operations`),
      get(`${base}/manyfast-baseline`), get(`${base}/tasks`), get(`${base}/audit?limit=100`), get(`${base}/deliveries`),
    ]);
    currentProject = detail.project;
    currentParticipants = participants.items;
    currentTasks = tasks.items;
    selectedDocumentVersion = baseline.version;
    document.getElementById("project-title").textContent = currentProject.name;
    document.getElementById("project-edit-name").value = currentProject.name;
    document.getElementById("project-edit-status").value = currentProject.status;
    document.getElementById("baseline").textContent = `ManyFast ${baseline.version}`;
    document.getElementById("task-document-version").value = baseline.version;
    document.getElementById("stats").innerHTML = [["요구사항",baseline.counts.requirements],["기능",baseline.counts.features],["상세 기능",baseline.counts.specs]].map(row => `<div class="stat"><strong>${row[1]}</strong>${row[0]}</div>`).join("");
    document.getElementById("gaps").innerHTML = baseline.known_gaps.map(value => `<div class="placeholder gap">보완 설계: ${esc(value)}</div>`).join("");
    const roles = Object.fromEntries(currentParticipants.map(row => [row.principal_id,row.role]));
    document.getElementById("agents").innerHTML = operations.agents.map(row => {
      const latest = row.latest ? esc(row.latest.status) : "최근 세션 없음";
      return `<div class="agent"><div class="agent-head"><strong>${esc(row.agent_id)}</strong><span class="status">${esc(row.state)}</span></div><div>${esc(roles[row.agent_id] || "participant")} · 세션 ${row.session_count}</div><small>${latest}</small></div>`;
    }).join("");
    renderTasks(); renderParticipants(); renderAudit(audit.items); applyAccess();
    renderQuickDeliveries(deliveries.items);
    document.getElementById("delivery-cards").innerHTML = deliveries.items.map(row => `<div class="project"><strong>${esc(row.agent_id)} · ${esc(row.status)}</strong><small>session/run ${esc(row.run_id || "-")} · retry ${row.attempt_count}/${row.max_attempts} · ${esc(row.error_code || "정상")}</small><div>${esc(row.response_message_id || "응답 대기")}</div>${demoMode && ["failed","timed_out"].includes(row.status) ? `<button onclick="retryDemoDelivery('${esc(row.id)}')">재시도</button>` : ""}</div>`).join("") || '<div class="empty">delivery 없음</div>';
    document.getElementById("stop-ack-delivery").innerHTML = '<option value="">현재 중지 cycle delivery 선택</option>' + deliveries.items.filter(row => row.status === "stopped" && row.stop_cycle_at === deliveries.stop_requested_at).map(row => `<option value="${esc(row.id)}">${esc(row.agent_id)} · ${esc(row.id.slice(0,8))}</option>`).join("");
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
  currentProject = null; currentParticipants = []; currentTasks = [];
  load();
}

function syncParticipantFlags() {
  const flags = {project_manager:[1,1,1,1],developer:[1,1,0,0],qa:[1,1,0,0],observer:[1,0,0,0]}[document.getElementById("participant-role").value];
  ["read","comment","approve","execute"].forEach((name,index) => { document.getElementById(`participant-${name}`).checked = Boolean(flags[index]); });
}

function editParticipant(principal) {
  const row = currentParticipants.find(item => item.principal_id === principal);
  if (!row) return;
  document.getElementById("participant-principal").value = row.principal_id;
  document.getElementById("participant-role").value = row.role;
  ["read","comment","approve","execute"].forEach(name => { document.getElementById(`participant-${name}`).checked = Boolean(row[`can_${name}`]); });
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
  document.getElementById("approval-flow").scrollIntoView({behavior:"smooth",block:"center"});
}

async function approveSelectedTask() {
  const out = document.getElementById("approval-result");
  try {
    const task = selectedTask("approval-task");
    const expires_at = Math.floor(Date.now() / 1000) + Number(document.getElementById("approval-expiry").value);
    const result = await post(`/api/war-room/tasks/${task.id}/approvals`, {decision:"approved",expires_at});
    out.textContent = `승인 완료 · ${result.approval_id.slice(0,8)}`; await load();
  } catch (error) { out.textContent = error.message; }
}

async function rejectSelectedTask() {
  const out = document.getElementById("approval-result");
  try { const task = selectedTask("approval-task"); await post(`/api/war-room/tasks/${task.id}/approvals`, {decision:"rejected"}); out.textContent = "거절 완료"; await load(); }
  catch (error) { out.textContent = error.message; }
}

async function deliverTask(id) {
  const out = document.getElementById("approval-result");
  try {
    const task = taskById(id);
    if (!task || !task.source_message_id) throw new Error("연결된 지시가 없습니다");
    const url = `/api/war-room/messages/${task.source_message_id}/deliveries`;
    const payload = {agent_ids: task.agent_ids || [task.assignee_agent_id], task_id: task.id};
    const result = await post(url, payload);
    out.textContent = `${payload.agent_ids.join(", ")} 전달 ${result.status}`; await load();
  } catch (error) { out.textContent = error.message; }
}

async function runSelectedTask() {
  const out = document.getElementById("approval-result");
  try {
    const task = selectedTask("approval-task");
    if (task.status === "approved") await post(`/api/war-room/tasks/${task.id}/transition`, {status:"running"});
    else if (task.status !== "running") throw new Error("승인된 작업만 실행할 수 있습니다");
    await load();
    const refreshed = taskById(task.id);
    if (refreshed?.source_message_id) await deliverTask(refreshed.id);
    else out.textContent = "실행 시작됨 · 연결된 지시는 없음";
  } catch (error) { out.textContent = error.message; }
}

async function saveResult() {
  const out = document.getElementById("qa-result");
  try {
    const task = selectedTask("qa-task");
    const body = document.getElementById("result-body").value.trim();
    if (!body) throw new Error("작업 결과를 입력하세요");
    const url = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/messages`;
    await post(url, {message_type:"result",body:`[task ${task.id}] ${body}`,source_message_id:task.source_message_id || null});
    document.getElementById("result-body").value = ""; out.textContent = "결과를 타임라인에 저장했습니다"; await load();
  } catch (error) { out.textContent = error.message; }
}

async function addEvidence() {
  const out = document.getElementById("qa-result");
  try {
    const task = selectedTask("qa-task");
    const body = {uri:document.getElementById("evidence-uri").value.trim(),summary:document.getElementById("evidence-summary").value.trim(),evidence_type:document.getElementById("evidence-type").value};
    const result = await post(`/api/war-room/tasks/${task.id}/evidence`, body);
    out.textContent = `증거 추가 · ${result.evidence_id.slice(0,8)}`; await loadAudit();
  } catch (error) { out.textContent = error.message; }
}

async function submitQaVerdict() {
  const out = document.getElementById("qa-result");
  try {
    const task = selectedTask("qa-task");
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
    const result = await post(url, {});
    out.textContent = `중지 요청 ${result.status}`;
    await load();
  }
  catch (error) { out.textContent = error.message; }
}

async function acknowledgeStop() {
  const out = document.getElementById("stop-result");
  try { const delivery_id=document.getElementById("stop-ack-delivery").value; if(!delivery_id) throw new Error("중지된 delivery를 선택하세요"); const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/stop-ack`, {delivery_id}); out.textContent = `중지 확인 ${result.status}`; await load(); }
  catch (error) { out.textContent = error.message; }
}

async function resumeProject() {
  const out = document.getElementById("stop-result");
  try { const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/resume`, {}); out.textContent = `재개 ${result.status}`; await load(); }
  catch (error) { out.textContent = `재개 실패 · ${error.message}`; }
}

async function changeTask(id, status) {
  try { await post(`/api/war-room/tasks/${id}/transition`, {status}); await load(); }
  catch (error) { alert(error.message); }
}

async function representativeComplete(id) {
  try { await post(`/api/war-room/tasks/${id}/representative-completion`, {decision:"approved"}); await load(); }
  catch (error) { alert(error.message); }
}

async function refreshTasks() { await load(); }

document.getElementById("task-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("task-result");
  try {
    const agent_ids=[...document.querySelectorAll('#task-agent-targets input:checked')].map(input=>input.value); const assignee_agent_id=document.getElementById("task-assignee").value; if(!agent_ids.includes(assignee_agent_id)) agent_ids.unshift(assignee_agent_id);
    const body = {scope:document.getElementById("task-scope").value,assignee_agent_id,agent_ids,call_limit:Number(document.getElementById("task-call-limit").value),turn_limit:Number(document.getElementById("task-turn-limit").value),deadline_at:Math.floor(Date.now()/1000)+Number(document.getElementById("task-deadline").value),document_version:document.getElementById("task-document-version").value};
    const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/tasks`, body);
    out.textContent = `생성됨 ${result.task_id.slice(0,8)}`; document.getElementById("task-scope").value = ""; await load();
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
    const instruction = document.getElementById("quick-instruction").value.trim();
    const agent_ids = [...document.querySelectorAll('#quick-agent-targets input:checked')].map(input => input.value);
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
      agent_ids,
      deadline_at: Math.floor(Date.now() / 1000) + 1800,
      document_version: selectedDocumentVersion,
    });
    quickTaskId = task.task_id;
    document.getElementById("quick-instruction").value = "";
    await post(`/api/war-room/tasks/${task.task_id}/approve-execute`, {expires_at:Math.floor(Date.now()/1000)+1800});
    await load();
    out.textContent = `${agent_ids.join(", ")}에게 작업을 시작했습니다. 결과 검토 단계에서 충돌과 다음 행동을 확인하세요.`;
  } catch (error) { out.textContent = `준비 실패: ${error.message}`; }
});

async function quickApproveAndRun() {
  const out = document.getElementById("quick-result");
  try {
    const task = taskById(quickTaskId);
    if (!task || !["awaiting_approval","running"].includes(task.status)) throw new Error("먼저 작업을 준비하세요");
    out.textContent = "승인하고 에이전트에게 전달하고 있습니다…";
    try {
      if (task.status === "awaiting_approval") await post(`/api/war-room/tasks/${task.id}/approve-execute`, {expires_at:Math.floor(Date.now()/1000)+1800});
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
  } catch (error) {
    out.textContent = `실행 실패: ${error.message}`;
  }
}

function quickOpenQa() {
  const task = taskById(quickTaskId);
  if (!task || task.status !== "qa") return;
  const area = document.getElementById("advanced-area");
  area.hidden = false;
  document.getElementById("qa-task").value = task.id;
  document.getElementById("qa-task").focus();
  document.getElementById("results-qa").scrollIntoView({behavior:"smooth",block:"center"});
}

async function quickCompleteTask() {
  const out = document.getElementById("quick-result");
  try {
    const task = taskById(quickTaskId);
    if (!task || task.status !== "qa") throw new Error("QA 단계 작업이 없습니다");
    await post(`/api/war-room/tasks/${task.id}/representative-completion`, {decision:"approved"});
    await load(); out.textContent = "최종 완료 처리했습니다.";
  } catch (error) { out.textContent = `완료 실패: ${error.message}`; }
}

async function quickProcessResults() {
  const out = document.getElementById("quick-result");
  try { out.textContent = "시연 응답을 처리하고 있습니다…"; await processDemoQueue(); out.textContent = "응답 처리가 끝났습니다. 결과 상태를 확인하세요."; }
  catch (error) { out.textContent = `응답 처리 실패: ${error.message}`; }
}

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
    out.textContent = `${principal_id} 저장 완료`; await load();
  } catch (error) { out.textContent = error.message; }
});

document.getElementById("message-form").addEventListener("submit", async event => {
  event.preventDefault(); const out = document.getElementById("message-result");
  try {
    const base = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}`;
    const taskId = document.getElementById("message-task").value;
    if (!taskId) throw new Error("작업을 먼저 생성하고 선택하세요");
    const result = await post(`${base}/instructions`, {task_id:taskId, body:document.getElementById("message-body").value});
    out.textContent = `선택 작업에 지시 연결 · ${result.task_id.slice(0,8)} · 승인 후 실행하세요`;
    document.getElementById("message-body").value = "";
    await load(); document.getElementById("approval-task").value = taskId;
  } catch (error) { out.textContent = `지시·작업 생성 실패: ${error.message}`; }
});

async function bindDemoSession(){const agent=document.getElementById("demo-session-agent").value; await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/participants/${agent}/test-session`,{session_key:document.getElementById("demo-session-key").value,session_id:document.getElementById("demo-session-id").value},"PUT"); await load();}
async function processDemoQueue(){await post('/api/war-room/demo/process',{}); await load();}
async function retryDemoDelivery(id){await post(`/api/war-room/deliveries/${id}/retry`,{}); await load();}
get('/api/war-room/demo-mode').then(()=>{demoMode=true;document.getElementById('demo-controls').hidden=false;}).catch(()=>{}).finally(load);
setInterval(() => { if (!document.querySelector("form:focus-within")) load(); }, 15000);
