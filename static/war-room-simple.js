let projects = [];
let project = null;
let access = null;
let tasks = [];
let board = [];
let documents = [];
let candidates = [];
let participants = [];
let baseline = null;
let deliveries = [];
let selectedProjectId = null;
let showAllDocuments = false;
let busy = false;
let generation = 0;
const WAR_ROOM_SIMPLE_ASSET_VERSION = "20260928-result-revalidation-v1";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

async function requestJson(url, options = {}) {
  const response = await fetch(url, {cache:"no-store", credentials:"same-origin", ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
const get = url => requestJson(url);
const post = (url, body = {}, method = "POST") => requestJson(url, {
  method,
  headers: {"Content-Type":"application/json", "Idempotency-Key":crypto.randomUUID()},
  body: JSON.stringify(body),
});

function statusInfo(task) {
  const pending = typeof taskDeliveryRows === "function" && task.status === "running"
    && taskDeliveryRows(task).some(row => row.status === "queued")
    && !taskDeliveryRows(task).some(row => ["sent","received"].includes(row.status));
  const state = task.processing_issues?.length ? "processing_review" : task.state === "system_error" ? "system_error" : pending ? "queued" : task.status;
  const map = {
    processing_review:["단계 확인 필요","waiting"], draft:["초안","muted"], awaiting_approval:["승인 대기","waiting"], approved:["실행 준비","waiting"],
    queued:["실행 대기","waiting"], running:["작업중","running"], qa:["검수중","qa"], completed:["완료","good"],
    rework_required:["재작업 필요","bad"], system_error:["문제 발생","bad"],
    stopped:["중지","muted"], stop_unconfirmed:["중지 확인 필요","bad"],
  };
  return map[state] || [state || "확인 필요","muted"];
}
function projectStatus(status) {
  return ({planning:"기획",active:"진행 중",paused:"일시중지",archived:"보관"})[status] || status || "-";
}
function fmtTime(epoch) {
  if (!epoch) return "-";
  return new Date(Number(epoch) * 1000).toLocaleString("ko-KR", {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});
}
function trim(text, max = 170) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > max ? value.slice(0, max - 1) + "…" : value;
}
function toast(message, tone = "") {
  const node = $("toast");
  node.textContent = message;
  node.style.display = "block";
  node.style.borderColor = tone === "bad" ? "#743c3c" : tone === "good" ? "#285d41" : "var(--line)";
  clearTimeout(window.__warToast);
  window.__warToast = setTimeout(() => node.style.display = "none", 4500);
}
function mutationContract(task) {
  return {contract_version:1, project_id:task.project_id || selectedProjectId, task_id:task.id, task_revision:Number(task.revision || 1)};
}
async function freshMutationContext(action, targetId) {
  const params = new URLSearchParams({action, target_id:targetId});
  return get(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/mutation-context?${params}`);
}
async function guardedPost(url, body, action, targetId) {
  const context = await freshMutationContext(action, targetId);
  return post(url, {...body, context_token:context.context_token});
}
function boardItem(taskId) { return board.find(row => row.task_id === taskId); }
function can(name) { return Boolean(access?.permissions?.includes(name)); }
function representative() { return access?.is_representative === true; }

function chooseProject(items) {
  const params = new URLSearchParams(location.search);
  const requested = params.get("project_id");
  const saved = localStorage.getItem("war-room-simple-project");
  return items.find(row => row.id === requested)?.id
    || items.find(row => row.id === saved)?.id
    || items.find(row => row.id === "plachem-agent-war-room")?.id
    || items.find(row => row.status !== "archived")?.id
    || items[0]?.id
    || null;
}

async function loadProjects() {
  const data = await get("/api/war-room/projects");
  projects = data.items || [];
  if (!selectedProjectId || !projects.some(row => row.id === selectedProjectId)) {
    selectedProjectId = chooseProject(projects);
  }
  renderProjectSelect();
  if (selectedProjectId) await loadProject(selectedProjectId);
}

function renderProjectSelect() {
  const select = $("project-select");
  if (!projects.length) {
    select.innerHTML = '<option value="">프로젝트 없음</option>';
    return;
  }
  const active = projects.filter(row => row.status !== "archived");
  const archived = projects.filter(row => row.status === "archived");
  let html = active.map(row => `<option value="${esc(row.id)}">${esc(row.name)}</option>`).join("");
  if (archived.length) {
    html += '<optgroup label="보관된 프로젝트">' + archived.map(row => `<option value="${esc(row.id)}">[보관] ${esc(row.name)}</option>`).join("") + "</optgroup>";
  }
  select.innerHTML = html;
  select.value = selectedProjectId;
}

async function loadProject(projectId) {
  const current = ++generation;
  const base = `/api/war-room/projects/${encodeURIComponent(projectId)}`;
  try {
    const [detail, accessData, taskData, candidateData, participantData, boardData, documentData, baselineData, deliveryData] = await Promise.all([
      get(base), get(`${base}/access`), get(`${base}/tasks`), get(`${base}/agent-candidates`),
      get(`${base}/participants`), get(`${base}/process-board`), get(`${base}/documents`),
      get(`${base}/manyfast-baseline`), get(`${base}/deliveries`),
    ]);
    if (current !== generation || projectId !== selectedProjectId) return;
    project = detail.project;
    access = accessData;
    tasks = taskData.items || [];
    candidates = candidateData.items || [];
    participants = participantData.items || [];
    board = boardData.items || [];
    documents = documentData.items || [];
    baseline = baselineData;
    deliveries = deliveryData.items || [];
    render();
  } catch (error) {
    toast(`프로젝트 조회 실패: ${error.message}`, "bad");
    $("project-summary").innerHTML = `<div class="notice bad">프로젝트 정보를 불러오지 못했습니다. ${esc(error.message)}</div>`;
  }
}

function render() {
  renderHeader();
  renderComposer();
  renderProjectSummary();
  renderActiveTasks();
  renderRecentResults();
  renderDocuments();
}

const OPEN_TASK_STATUSES = new Set(["awaiting_approval","approved","running","qa","rework_required","stopped","stop_unconfirmed"]);
const CURRENT_WINDOW_SECONDS = 72 * 60 * 60;

function hasLiveDelivery(taskId) {
  return deliveries.some(row => row.task_id === taskId && ["queued","sent","received"].includes(row.status));
}
function isCurrentTask(task) {
  if (hasLiveDelivery(task.id)) return true;
  const updated = Number(task.updated_at || 0);
  return updated > 0 && (Date.now() / 1000 - updated) <= CURRENT_WINDOW_SECONDS;
}
function openTasks() {
  return tasks.filter(task => OPEN_TASK_STATUSES.has(task.status) || task.state === "system_error");
}
function currentOpenTasks() {
  return openTasks().filter(isCurrentTask);
}
function taskCounts() {
  const current = currentOpenTasks();
  const running = current.filter(t => t.status === "running").length;
  const approval = current.filter(t => ["awaiting_approval","approved"].includes(t.status)).length;
  const issues = current.filter(t => t.state === "system_error" || ["rework_required","stop_unconfirmed"].includes(t.status)).length;
  const qa = current.filter(t => t.status === "qa").length;
  const stale = Math.max(0, openTasks().length - current.length);
  return {running, approval, issues, qa, stale};
}

function renderHeader() {
  const counts = taskCounts();
  $("stat-running").textContent = counts.running;
  $("stat-approval").textContent = counts.approval;
  $("stat-issues").textContent = counts.issues;
  $("stat-documents").textContent = documents.length;
  $("project-status").textContent = `상태: ${projectStatus(project?.status)}`;
  $("last-refresh").textContent = `갱신 ${new Date().toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit"})}`;
  $("auth-pill").textContent = representative() ? "대표 승인 가능" : can("manage") ? "관리 권한 · 대표 승인 별도" : "읽기 전용";
  $("advanced-link").href = `/war-room/advanced?project_id=${encodeURIComponent(selectedProjectId)}&screen=project`;
  localStorage.setItem("war-room-simple-project", selectedProjectId);
  const url = new URL(location.href);
  url.searchParams.set("project_id", selectedProjectId);
  history.replaceState(null, "", url);
}

function participantFor(agentId) {
  return participants.find(row => row.principal_type === "agent" && row.principal_id === agentId);
}
function eligibleAssignees() {
  const preferred = ["ERPmanager","ERPcoder","researcher","processdata","processsupport","secretary","qwentest","producer","cliper","main"];
  return candidates
    .filter(row => row.execution_eligible && row.enabled && !(row.capabilities || []).includes("qa"))
    .sort((a,b) => {
      const ai = preferred.indexOf(a.agent_id), bi = preferred.indexOf(b.agent_id);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.agent_id.localeCompare(b.agent_id);
    });
}
function eligibleReviewers() {
  return candidates.filter(row => row.enabled && (row.capabilities || []).includes("qa"));
}
function agentOptionLabel(row) {
  const participant = participantFor(row.agent_id);
  if (!participant) return `${row.agent_id} · 선택 시 프로젝트 참여`;
  if (!participant.active) return `${row.agent_id} · 선택 시 참여 재활성화`;
  return row.agent_id;
}
function setOptions(select, rows, preferredId, emptyText) {
  if (!rows.length) {
    select.innerHTML = `<option value="">${esc(emptyText)}</option>`;
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const previous = select.value;
  select.innerHTML = rows.map(row => `<option value="${esc(row.agent_id)}">${esc(agentOptionLabel(row))}</option>`).join("");
  if (rows.some(row => row.agent_id === previous)) select.value = previous;
  else if (rows.some(row => row.agent_id === preferredId)) select.value = preferredId;
}

function renderComposer() {
  const assignees = eligibleAssignees();
  const reviewers = eligibleReviewers();
  setOptions($("assignee"), assignees, "ERPmanager", "수행 Agent 없음");
  setOptions($("reviewer"), reviewers, "ERPqa", "QA Agent 없음");
  const archived = project?.status === "archived";
  const allowed = can("manage") && assignees.length && reviewers.length && !archived;
  $("prepare-button").disabled = !allowed;
  $("instruction").disabled = !allowed;
  if (archived) $("composer-note").textContent = "보관된 프로젝트는 읽기 전용입니다.";
  else if (!can("manage")) $("composer-note").textContent = "이 프로젝트에서는 작업을 만들 권한이 없습니다.";
  else if (!reviewers.length) $("composer-note").textContent = "사용 가능한 QA Agent가 없습니다. 고급 관리에서 Agent 등록 상태를 확인하십시오.";
  else if (!assignees.length) $("composer-note").textContent = "서버에 실행 가능한 담당 Agent가 없습니다. Agent 등록 상태를 확인하십시오.";
  else $("composer-note").textContent = "서버의 전체 가용 Agent를 선택할 수 있습니다. 프로젝트 미참여 Agent는 작업 준비 시 자동으로 참여 등록됩니다.";
}

async function ensureProjectParticipant(agentId, role) {
  const current = participantFor(agentId);
  const base = `/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/participants`;
  if (!current) {
    await post(base, {principal_id:agentId, role});
    return "added";
  }
  if (role === "qa" && (current.role !== "qa" || !current.active)) {
    await post(`${base}/${encodeURIComponent(agentId)}`, {role:"qa", active:true}, "PATCH");
    return "updated";
  }
  if (!current.active) {
    await post(`${base}/${encodeURIComponent(agentId)}`, {active:true}, "PATCH");
    return "reactivated";
  }
  return "unchanged";
}

function renderProjectSummary() {
  const counts = taskCounts();
  const notices = [];
  if (representative()) notices.push(['good',"대표 인증이 확인되어 승인·실행·최종 승인을 사용할 수 있습니다."]);
  else notices.push(['warn',"현재 접속은 대표 최종 승인 권한이 아닙니다. 조회와 허용된 관리 기능만 표시합니다."]);
  if (counts.approval) notices.push(['warn',`대표 승인 대기 작업이 ${counts.approval}건 있습니다.`]);
  if (counts.issues) notices.push(['bad',`재작업 또는 오류 확인이 필요한 작업이 ${counts.issues}건 있습니다.`]);
  if (counts.qa) notices.push(['',`검수 중인 작업이 ${counts.qa}건 있습니다.`]);
  if (counts.stale) notices.push(['',`72시간 이상 갱신되지 않은 미정리 작업 ${counts.stale}건은 Simple 화면에서 숨겼습니다. 필요하면 고급 관리에서 확인할 수 있습니다.`]);
  if (!counts.running && !counts.approval && !counts.issues && !counts.qa) notices.push(['good',"현재 즉시 확인이 필요한 작업이 없습니다."]);
  $("project-summary").innerHTML = notices.map(([tone,text]) => `<div class="notice ${tone}">${esc(text)}</div>`).join("");
}

function taskDeliveryRows(task) {
  return deliveries
    .filter(row => (row.task_id === task.id || row.message_id === task.source_message_id)
      && Number(row.task_revision || 1) === Number(task.revision || 1))
    .sort((a,b) => Number(b.created_at || 0) - Number(a.created_at || 0));
}

function failureDiagnosis(task) {
  const issue = (task.processing_issues || [])[0];
  if (issue) return {stage:issue.stage === "QA" ? "QA 처리" : issue.stage === "RECONCILE" ? "실행 상태 확인" : "결과 확인",
    title:"업무 결과는 보존되어 있습니다.", cause:issue.code, next:issue.stage === "QA" ? "Worker 재실행 없이 QA만 재개하십시오." : "해당 단계의 오류를 확인해야 합니다. 전체 작업은 자동 재실행하지 않습니다.",
    workerCompleted:taskDeliveryRows(task).some(row => row.response_body || row.raw_response), code:issue.code, summary:""};
  const row = taskDeliveryRows(task).find(item =>
    ["failed","timed_out"].includes(item.status) || item.run_status === "FAIL" || item.error_code || item.validation_error || item.cancel_reason
  );
  if (!row && task.status !== "rework_required" && task.state !== "system_error") return null;
  const code = row?.validation_error || row?.error_code || row?.cancel_reason || "QA_REWORK_REQUIRED";
  const workerCompleted = Boolean(row?.response_body || row?.raw_response || row?.result_summary);
  if (String(code).includes("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED")) {
    return {stage:"FastGateway 결과 검증", title:"Worker 응답은 왔지만 Evidence 검증에서 실패했습니다.", cause:"Worker가 보고한 증거 문구를 실제 실행 기록과 일치한다고 확인하지 못해 FastGateway가 결과를 거절했습니다.", next:"검증 규칙 또는 Evidence 내용을 확인한 뒤 재승인·재실행하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
  }
  if (String(code).includes("EVIDENCE_CONTRADICTION")) {
    return {stage:"FastGateway 결과 검증", title:"Worker 보고와 실제 실행 기록이 서로 모순됩니다.", cause:"하지 않았다고 보고한 행동이 실행 기록에서 발견됐거나 그 반대 상황입니다.", next:"Worker 원본 응답과 실행 기록을 확인한 뒤 재작업하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
  }
  if (row?.status === "timed_out") {
    return {stage:"Agent 실행", title:"Agent가 제한시간 안에 응답하지 못했습니다.", cause:"실행은 시작됐지만 최종 결과가 제한시간 내 도착하지 않았습니다.", next:"Agent 상태를 확인하고 재승인·재실행하거나 다른 Agent로 변경하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
  }
  if (String(code).includes("agent_busy")) {
    return {stage:"Agent 실행 대기", title:"Agent가 다른 작업을 처리 중입니다.", cause:"동일 Agent의 활성 작업 때문에 이번 작업이 바로 실행되지 못했습니다.", next:"기존 작업 종료를 기다리거나 다른 Agent를 선택하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
  }
  if (task.status === "rework_required" && task.latest_qa_verdict?.verdict) {
    return {stage:"QA 검수", title:`QA가 ${task.latest_qa_verdict.verdict} 판정을 내려 재작업이 필요합니다.`, cause:"QA 검수 결과 현재 결과를 최종 승인할 수 없습니다.", next:"결과와 Evidence를 수정한 뒤 재승인·재실행하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
  }
  return {stage:"작업 처리", title:"작업이 정상 완료되지 않았습니다.", cause:"실행 또는 검증 단계에서 오류가 발생했습니다.", next:"아래 기술 상세를 확인한 뒤 재승인·재실행하십시오.", workerCompleted, code, summary:row?.result_summary || ""};
}

function failureBox(task) {
  const info = failureDiagnosis(task);
  if (!info) return "";
  const worker = info.workerCompleted ? "Worker 응답: 수신 완료" : "Worker 응답: 완료 확인 안 됨";
  return `<div class="failure-box"><strong>실패 원인 · ${esc(info.stage)}</strong><div class="cause">${esc(info.title)}<br>${esc(info.cause)}</div>${info.summary ? `<div class="result-summary">Worker 결과: ${esc(trim(info.summary,220))}</div>` : ""}<div class="next">${esc(worker)} · 다음 조치: ${esc(info.next)}</div><details><summary>기술 상세</summary><code>${esc(info.code)}</code></details></div>`;
}

function taskCard(task) {
  const [label,tone] = statusInfo(task);
  const qa = task.latest_qa_verdict?.verdict;
  const contract = task.execution_contract || {};
  const profileName = ({ACKNOWLEDGEMENT:"수신·연결 시험", READ_ONLY:"조회·분석", CODE_CHANGE:"코드 수정"})[contract.task_profile];
  const contractText = profileName ? `${profileName} · Worker ${Math.floor(contract.worker_timeout_seconds/60)}분 · QA ${Math.floor(contract.qa_timeout_seconds/60)}분` : "기존 작업 조건";
  const meta = [task.assignee_agent_id && `담당 ${task.assignee_agent_id}`, task.reviewer_agent_id && `QA ${task.reviewer_agent_id}`, `수정 ${fmtTime(task.updated_at)}`].filter(Boolean).join(" · ");
  const actions = [];
  if (task.status === "awaiting_approval") {
    actions.push(`<button class="btn primary" data-rep-action onclick="approveAndRun('${esc(task.id)}')" ${representative() ? "" : "disabled"}>승인·실행</button>`);
  }
  if (task.status === "approved") {
    actions.push(`<button class="btn primary" data-rep-action onclick="runApproved('${esc(task.id)}')" ${representative() ? "" : "disabled"}>실행</button>`);
  }
  if (task.status === "qa" && (task.processing_issues || []).some(issue => issue.stage === "QA")) {
    actions.push(`<button class="btn primary" onclick="resumeQA('${esc(task.id)}')" ${representative() ? "" : "disabled"}>QA만 재개</button>`);
  }
  if (["running","qa"].includes(task.status)) {
    actions.push(`<button class="btn danger" data-rep-action onclick="stopTask('${esc(task.id)}')" ${representative() ? "" : "disabled"}>중지</button>`);
  }
  if (["rework_required","stopped","stop_unconfirmed"].includes(task.status) && can("manage")) {
    actions.push(`<button class="btn" onclick="prepareReapproval('${esc(task.id)}')">재승인 준비</button>`);
  }
  if (["running", "qa", "rework_required"].includes(task.status) && task.revalidation?.eligible) {
    actions.push(`<button class="btn primary" onclick="revalidateResult('${esc(task.id)}')" ${representative() ? "" : "disabled"}>결과만 재검증</button>`);
  }
  if (task.status === "qa" && qa === "PASS" && Number(task.evidence_count || 0) > 0) {
    actions.push(`<button class="btn primary" data-rep-action onclick="completeTask('${esc(task.id)}')" ${representative() ? "" : "disabled"}>최종 승인</button>`);
  }
  actions.push(`<a class="btn" href="/war-room/advanced?project_id=${encodeURIComponent(selectedProjectId)}&task_id=${encodeURIComponent(task.id)}&screen=task">고급 상세</a>`);
  return `<div class="task">
    <div class="task-top"><div><div class="task-title">${esc(trim(task.scope,190))}</div><div class="task-meta">${esc(meta)}${qa ? ` · QA ${esc(qa)}` : ""}</div></div><span class="status ${tone}">${esc(label)}</span></div>
    <div class="task-meta">${esc(contractText)}</div>
    ${contract.completion_conditions?.length ? `<div class="task-meta">완료 조건: ${esc(contract.completion_conditions.join(" / "))}</div>` : ""}
    ${failureBox(task)}
    <div class="task-actions">${actions.join("")}</div>
  </div>`;
}

function renderActiveTasks() {
  const active = currentOpenTasks()
    .sort((a,b) => Number(b.updated_at||0)-Number(a.updated_at||0))
    .slice(0,8);
  $("active-count").textContent = active.length ? `최근 ${active.length}건` : "";
  $("active-tasks").innerHTML = active.length ? active.map(taskCard).join("") : '<div class="empty">현재 진행 중이거나 확인이 필요한 작업이 없습니다.</div>';
}

function parseResult(output) {
  if (!output) return "";
  if (typeof output === "object") return trim(output.summary || JSON.stringify(output), 260);
  const text = String(output).trim();
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object") return trim(value.summary || value.result_summary || text, 260);
  } catch (_) {}
  return trim(text, 260);
}
function renderRecentResults() {
  const resultCutoff = Date.now() / 1000 - (7 * 24 * 60 * 60);
  const items = board
    .filter(row => Number(row.updated_at || 0) >= resultCutoff)
    .filter(row => row.output || ["PASS","FAIL","REWORK","DONE","BLOCKED"].includes(row.mapped_state))
    .sort((a,b) => Number(b.updated_at||0)-Number(a.updated_at||0))
    .slice(0,5);
  $("recent-results").innerHTML = items.length ? items.map(row => {
    const task = tasks.find(t => t.id === row.task_id);
    const mapped = ({PASS:["검수 통과","good"],DONE:["완료","good"],FAIL:["검수 실패","bad"],REWORK:["재작업","bad"],BLOCKED:["문제 발생","bad"],RUNNING:["작업중","running"]})[row.mapped_state] || [row.mapped_state,"muted"];
    const summary = parseResult(row.output) || "결과 요약이 아직 없습니다.";
    const complete = task?.status === "qa" && task?.latest_qa_verdict?.verdict === "PASS" && Number(task?.evidence_count||0)>0;
    return `<div class="task"><div class="task-top"><div><div class="task-title">${esc(trim(row.input || task?.scope || "작업",170))}</div><div class="task-meta">${esc(row.assigned_agent || task?.assignee_agent_id || "-")} · ${fmtTime(row.updated_at)}</div></div><span class="status ${mapped[1]}">${esc(mapped[0])}</span></div><div class="result-summary">${esc(summary)}</div><div class="task-actions">${complete ? `<button class="btn primary" onclick="completeTask('${esc(row.task_id)}')" ${representative() ? "" : "disabled"}>최종 승인</button>` : ""}<a class="btn" href="/war-room/advanced?project_id=${encodeURIComponent(selectedProjectId)}&task_id=${encodeURIComponent(row.task_id)}&screen=task">결과 상세</a></div></div>`;
  }).join("") : '<div class="empty">최근 7일 결과가 없습니다. 이전 결과는 고급 관리에서 확인할 수 있습니다.</div>';
}

function renderDocuments() {
  const sorted = [...documents].sort((a,b) => Number(b.updated_at||0)-Number(a.updated_at||0));
  const visible = showAllDocuments ? sorted : sorted.slice(0,4);
  $("documents-toggle").textContent = showAllDocuments ? "간단히 보기" : `전체 보기 (${documents.length})`;
  $("documents").innerHTML = visible.length ? visible.map(doc => `<div class="doc"><strong>${esc(doc.title)}</strong><small>${esc(doc.category)} · v${esc(doc.current_version)}</small><small>${esc(trim(doc.summary || "",100))}</small></div>`).join("") : '<div class="empty" style="grid-column:1/-1">아직 등록된 프로젝트 문서가 없습니다.</div>';
}

async function latestTask(taskId) {
  const data = await get(`/api/war-room/tasks/${encodeURIComponent(taskId)}`);
  if (!data.task || data.task.project_id !== selectedProjectId) throw new Error("작업이 현재 프로젝트와 일치하지 않습니다.");
  return data.task;
}
async function withBusy(fn) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(node => node.setAttribute("aria-busy","true"));
  try { await fn(); }
  finally { busy = false; document.querySelectorAll("button").forEach(node => node.removeAttribute("aria-busy")); }
}

async function approveAndRun(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      if (task.status !== "awaiting_approval") throw new Error("이미 상태가 변경되었습니다.");
      await guardedPost(
        `/api/war-room/tasks/${encodeURIComponent(task.id)}/approve-execute`,
        {...mutationContract(task), expires_at:Math.floor(Date.now()/1000)+3600},
        "task_approve_execute", task.id
      );
      toast("대표 승인 후 작업 실행을 시작했습니다.", "good");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`실행 실패: ${error.message}`, "bad"); }
  });
}
async function runApproved(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      if (task.status !== "approved") throw new Error("실행 준비 상태가 아닙니다.");
      await post(`/api/war-room/tasks/${encodeURIComponent(task.id)}/transition`, {...mutationContract(task), status:"running"});
      toast("작업 실행 상태로 전환했습니다.", "good");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`실행 실패: ${error.message}`, "bad"); }
  });
}
async function stopTask(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      await guardedPost(`/api/war-room/tasks/${encodeURIComponent(task.id)}/stop`, {}, "task_stop", task.id);
      toast("작업 중지 요청을 보냈습니다.");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`중지 실패: ${error.message}`, "bad"); }
  });
}
async function prepareReapproval(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      if (!["rework_required","stopped","stop_unconfirmed"].includes(task.status)) throw new Error("재승인 준비 대상 상태가 아닙니다.");
      await post(`/api/war-room/tasks/${encodeURIComponent(task.id)}/transition`, {
        ...mutationContract(task), status:"awaiting_approval", deadline_at:Math.floor(Date.now()/1000)+9000
      });
      toast("재승인 준비가 끝났습니다. 대표 승인 후 다시 실행할 수 있습니다.", "good");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`재승인 준비 실패: ${error.message}`, "bad"); }
  });
}
async function resumeQA(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      const result = await guardedPost(`/api/war-room/tasks/${encodeURIComponent(task.id)}/resume-qa`,
        mutationContract(task), "task_resume_qa", task.id);
      toast(result.status === "qa_unavailable" ? "QA 연결이 아직 복구되지 않았습니다. Worker 결과는 보존됩니다." : "Worker 재실행 없이 QA를 재개했습니다.");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`QA 재개 실패: ${error.message}`, "bad"); }
  });
}
async function revalidateResult(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      if (!["running", "qa", "rework_required"].includes(task.status) || !task.revalidation?.eligible) throw new Error("재검증 대상 결과가 없습니다.");
      const result = await guardedPost(
        `/api/war-room/tasks/${encodeURIComponent(task.id)}/revalidate-result`,
        {...mutationContract(task), core_run_id:task.revalidation.core_run_id},
        "task_revalidate_result", task.id
      );
      toast(result.outcome === "PASS" ? "결과만 재검증하여 QA 단계로 이동했습니다." : "결과 재검증이 통과하지 못했습니다.", result.outcome === "PASS" ? "good" : "bad");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`결과 재검증 실패: ${error.message}`, "bad"); }
  });
}

async function completeTask(taskId) {
  await withBusy(async () => {
    try {
      const task = await latestTask(taskId);
      if (task.status !== "qa" || task.latest_qa_verdict?.verdict !== "PASS" || Number(task.evidence_count||0)<1) {
        throw new Error("QA PASS와 필수 증거가 아직 충족되지 않았습니다.");
      }
      await guardedPost(`/api/war-room/tasks/${encodeURIComponent(task.id)}/representative-completion`, {decision:"approved"}, "representative_completion", task.id);
      toast("대표 최종 승인으로 작업을 완료했습니다.", "good");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`최종 승인 실패: ${error.message}`, "bad"); }
  });
}

$("task-form").addEventListener("submit", async event => {
  event.preventDefault();
  await withBusy(async () => {
    try {
      const instruction = $("instruction").value.trim();
      const assignee = $("assignee").value;
      const reviewer = $("reviewer").value;
      if (!instruction || !assignee || !reviewer) throw new Error("작업 내용, 담당 Agent, QA Agent를 확인하십시오.");
      if (assignee === reviewer) throw new Error("담당 Agent와 QA Agent는 서로 달라야 합니다.");
      const assigneeJoin = await ensureProjectParticipant(assignee, "developer");
      const reviewerJoin = await ensureProjectParticipant(reviewer, "qa");
      const result = await post(`/api/war-room/projects/${encodeURIComponent(selectedProjectId)}/prepare`, {
        instruction, scope:instruction, assignee_agent_id:assignee, reviewer_agent_id:reviewer,
        agent_ids:[assignee], execution_mode:"FAST_GATEWAY",
        task_profile:$("task-profile").value,
        deadline_at:Math.floor(Date.now()/1000)+9000,
        document_version:baseline?.version || project?.manyfast_version || "unknown",
      });
      $("instruction").value = "";
      const joined = [assigneeJoin, reviewerJoin].some(value => value !== "unchanged");
      toast(`작업 준비 완료 · ${result.task_id.slice(0,8)} · 아직 실행 전입니다.${joined ? " 선택한 Agent의 프로젝트 참여도 자동 반영했습니다." : ""}`, "good");
      await loadProject(selectedProjectId);
    } catch (error) { toast(`작업 준비 실패: ${error.message}`, "bad"); }
  });
});

$("project-select").addEventListener("change", async event => {
  selectedProjectId = event.target.value;
  showAllDocuments = false;
  await loadProject(selectedProjectId);
});
$("documents-toggle").addEventListener("click", () => { showAllDocuments = !showAllDocuments; renderDocuments(); });

async function refreshAll() {
  if (!selectedProjectId || busy) return;
  await loadProject(selectedProjectId);
  toast("최신 상태로 갱신했습니다.");
}

loadProjects().catch(error => toast(`War Room 시작 실패: ${error.message}`, "bad"));
setInterval(() => {
  if (!busy && !document.querySelector("form:focus-within") && selectedProjectId) loadProject(selectedProjectId);
}, 15000);