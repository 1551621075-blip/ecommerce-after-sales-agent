"use strict";

// Credentials and unresolved commands live only in this page's memory.
const state = {token: "", user: null, sid: null, view: null, ticket: null, busy: false,
  pending: null, needsReload: false, generation: 0, proposalActions: new Map()};
const $ = (id) => document.getElementById(id);
const actionId = () => `web-${crypto.randomUUID()}`;
const text = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
function node(tag, value, className) {
  const result = document.createElement(tag);
  if (value !== undefined) result.textContent = text(value);
  if (className) result.className = className;
  return result;
}
function status(message, error = false) {
  $("status").textContent = message;
  $("status").className = error ? "status error" : "status";
}
function guarded(fn) {
  return async (event) => {
    if (event) event.preventDefault();
    try { await fn(event); } catch (error) { status(error.message || "操作失败，请重新加载核实。", true); }
  };
}
function button(label, handler, write = false) {
  const result = node("button", label);
  result.type = "button";
  if (write) result.dataset.write = "";
  result.addEventListener("click", guarded(handler));
  return result;
}
function lockWrites() {
  const disabled = state.busy || Boolean(state.pending) || state.needsReload;
  document.querySelectorAll("[data-write]").forEach((element) => { element.disabled = disabled; });
  $("retry").disabled = state.busy;
  $("logout").disabled = state.busy || Boolean(state.pending);
}
class RequestError extends Error {
  constructor(message, code = 0) { super(message); this.code = code; }
}
async function request(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST", mode: "same-origin", credentials: "omit",
      cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal,
      headers: {Authorization: `Bearer ${state.token}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    let result;
    try { result = await response.json(); } catch (_) { throw new RequestError("无法确认服务器回执，请核实原操作。", response.status >= 500 ? response.status : 0); }
    if (!response.ok) throw new RequestError(result.error?.message || "请求被拒绝。", response.status);
    return result;
  } catch (error) {
    if (error instanceof RequestError) throw error;
    throw new RequestError("网络中断或超时，操作结果不明。请使用原 action_id 核实。", 0);
  } finally { clearTimeout(timer); }
}
function operationInfo(operation) {
  return {path: operation.path, action_id: operation.body.action_id,
    expected_version: operation.body.expected_version, content_hash: operation.body.content_hash,
    decision: operation.body.decision};
}
async function mutate(path, values, id) {
  if (state.busy || state.pending || state.needsReload) throw new Error("请先处理未确认操作或重新加载最新版本。");
  const operation = {path, body: {action_id: id || actionId(), ...values}};
  return sendOperation(operation);
}
async function sendOperation(operation) {
  state.busy = true;
  $("last-operation").hidden = false;
  $("operation-info").textContent = text(operationInfo(operation));
  lockWrites();
  try {
    const result = await request(operation.path, operation.body);
    state.pending = null;
    $("pending").hidden = true;
    if (operation.path === "/api/sessions" && result.id) state.sid = result.id;
    if (operation.path.endsWith("/messages")) $("message").value = "";
    if (operation.path.endsWith("/claim")) state.ticket = {id: result.id};
    if (operation.path.endsWith("/end")) state.ticket = null;
    status("已收到服务器回执。所有审批仅为本地Demo，不执行真实支付或履约。");
    try { await reload(false); } catch (_) { state.needsReload = true; status("操作已收到回执，但最新视图加载失败；请重新加载。", true); }
    return result;
  } catch (error) {
    if (error.code === 0 || error.code >= 500) {
      state.pending = operation;
      $("pending").hidden = false;
      $("pending-info").textContent = text(operationInfo(operation));
    } else {
      state.pending = null;
      $("pending").hidden = true;
      if (error.code === 409) {
        state.needsReload = true;
        error.message += " 请点击重新加载最新版本，并重新核对后操作。";
      }
    }
    throw error;
  } finally { state.busy = false; lockWrites(); }
}
function sourceCard(title, value) {
  const card = node("details", undefined, "source");
  card.append(node("summary", title), node("pre", value));
  return card;
}
function money(cents) {
  if (!Number.isSafeInteger(cents) || cents < 0) return "金额超出浏览器精确显示范围；请人工核实，不可据此确认";
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")} 元 CNY（${cents}分）`;
}
function renderSessions(sessions) {
  $("sessions").replaceChildren();
  for (const session of sessions) {
    $("sessions").append(button(`${session.id} · v${session.version}`, async () => {
      state.sid = session.id;
      await loadSession();
    }));
  }
  if (!sessions.length) $("sessions").append(node("p", "暂无会话，请新建。", "muted"));
}
async function loadSession() {
  if (!state.sid) return;
  const generation = state.generation, sid = state.sid;
  const view = await request(`/api/sessions/${sid}`);
  if (generation !== state.generation || sid !== state.sid) return;
  state.view = view;
  renderSession(view);
  const metrics = await request(`/api/sessions/${sid}/metrics`);
  if (generation === state.generation && sid === state.sid) $("metrics").textContent = text(metrics);
}
function renderSession(view) {
  const session = view.session;
  $("session-title").textContent = session.id;
  $("session-meta").textContent = `来源会话版本 v${session.version} · 控制状态 ${session.payload.control}`;
  $("messages").replaceChildren();
  for (const message of session.payload.messages || []) {
    const label = message.role === "customer" ? "客户陈述" : "对话内容（非审批或付款证明）";
    const bubble = node("article", undefined, message.role === "customer" ? "bubble customer" : "bubble");
    bubble.append(node("strong", label), node("p", message.text ?? message));
    $("messages").append(bubble);
  }
  $("run-traces").replaceChildren();
  for (const run of view.runs || []) {
    const section = node("section");
    section.append(node("p", `${run.id} · ${run.payload.state}`));
    for (const [index, step] of (run.payload.trace || []).entries()) {
      section.append(sourceCard(`第 ${index + 1} 步 · ${step.tool}`, {
        action: step.action || step.proposed_action || null, observation: step.observation,
        elapsed_seconds: step.elapsed ?? null,
      }));
    }
    $("run-traces").append(section);
  }
  $("evidence").replaceChildren();
  for (const evidence of view.evidences || []) {
    $("evidence").append(sourceCard(`${evidence.id} · v${evidence.version} · 来源证据`, evidence));
  }
  $("proposals").replaceChildren();
  for (const proposal of view.proposals || []) renderProposal(proposal);
  if (!(view.proposals || []).length) $("proposals").append(node("p", "尚无后端评估产生的方案。", "muted"));
  $("applications").replaceChildren();
  for (const application of view.applications || []) {
    const card = node("article", undefined, "card");
    const payload = application.payload;
    card.append(node("h4", application.id), node("p", `来源状态：${payload.state} · v${application.version}`),
      node("p", `方案类型：${payload.option.kind}；商品 ${payload.option.item_id} × ${payload.option.quantity}`),
      node("p", `申请金额：${money(payload.option.amount_cents)}`), node("p", "未执行真实退款或履约。"));
    if (payload.decision_note) card.append(node("p", `人工备注：${payload.decision_note}`));
    if (payload.state === "submitted_pending_review") {
      card.append(button("撤回未决申请", () => mutate(`/api/applications/${application.id}/withdraw`,
        {expected_version: application.version}), true));
    }
    $("applications").append(card);
  }
  lockWrites();
}
function renderProposal(proposal) {
  const card = node("article", undefined, "card proposal"), payload = proposal.payload, option = payload.option;
  const key = `${proposal.id}:${proposal.version}:${proposal.content_hash}`;
  if (!state.proposalActions.has(key)) state.proposalActions.set(key, actionId());
  const id = state.proposalActions.get(key);
  card.append(node("h4", `后端精确方案 · ${proposal.id}`),
    node("p", `订单 ${payload.order_id}；商品 ${option.item_id} × ${option.quantity}`),
    node("p", `类型：${option.kind}；仅商品实付申请金额：${money(option.amount_cents)}`, "amount"),
    node("p", `来源状态：${payload.state}${proposal.display_expired ? "（已过期）" : ""}`),
    node("pre", {version: proposal.version, content_hash: proposal.content_hash, action_id: id}),
    node("p", payload.notice || "需人工终审，当前未退款。"));
  const list = node("ul");
  for (const condition of option.conditions || []) list.append(node("li", condition));
  card.append(list, node("p", `政策引用：${(option.policy_ids || []).join("、")}`));
  if (payload.state === "pending_confirmation" && !proposal.display_expired && Number.isSafeInteger(option.amount_cents)) {
    const controls = node("div", undefined, "actions");
    for (const [decision, label] of [["confirm", "核对后确认提交申请"], ["decline", "拒绝此方案"]]) {
      controls.append(button(label, () => mutate(`/api/proposals/${proposal.id}/decision`,
        {expected_version: proposal.version, content_hash: proposal.content_hash, decision}, id), true));
    }
    card.append(controls);
  }
  $("proposals").append(card);
}
async function loadQueue() {
  const generation = state.generation;
  const queue = await request("/api/queue");
  if (generation !== state.generation) return;
  $("queue").replaceChildren();
  for (const ticket of queue) {
    const card = node("article", undefined, "card");
    card.append(node("h4", ticket.id), node("p", `${ticket.category} · ${ticket.state} · v${ticket.version}`));
    if (ticket.state === "queued") {
      card.append(button("领取工单", () => mutate(`/api/tickets/${ticket.id}/claim`,
        {expected_version: ticket.version}), true));
    } else {
      card.append(button("查看我已领取的详情", async () => { state.ticket = {id: ticket.id}; await loadTicket(); }));
    }
    $("queue").append(card);
  }
  if (!queue.length) $("queue").append(node("p", "当前无可见工单。", "muted"));
  lockWrites();
}
async function loadTicket() {
  if (!state.ticket) { $("ticket").replaceChildren(); return; }
  const generation = state.generation, id = state.ticket.id;
  const detail = await request(`/api/tickets/${id}`);
  if (generation !== state.generation || state.ticket?.id !== id) return;
  state.ticket = detail.ticket;
  const target = $("ticket"), ticket = detail.ticket;
  target.replaceChildren(node("h4", ticket.id), node("p", `来源工单状态：${ticket.payload.state} · v${ticket.version}`));
  for (const [key, value] of Object.entries(detail)) target.append(sourceCard(key, value));
  const application = detail.application;
  if (application) {
    target.append(node("p", `来源申请状态：${application.payload.state}；商品申请金额：${money(application.payload.option.amount_cents)}`));
  }
  const label = node("label", "人工审核或结束备注"), note = node("textarea");
  note.id = "review-note"; note.maxLength = 2000; note.rows = 3;
  label.htmlFor = note.id;
  target.append(label, note);
  function noteValue() {
    const value = note.value.trim();
    if (!value) throw new Error("请填写人工备注。");
    return value;
  }
  if (application?.payload.state === "submitted_pending_review") {
    for (const [decision, labelText] of [["approved_demo", "记录Demo批准（不打款）"], ["rejected", "记录拒绝"]]) {
      target.append(button(labelText, () => mutate(`/api/tickets/${ticket.id}/review`,
        {expected_version: ticket.version, decision, note: noteValue()}), true));
    }
  } else {
    target.append(button("结束人工接管并交回会话", () => mutate(`/api/tickets/${ticket.id}/end`,
      {expected_version: ticket.version, note: noteValue()}), true));
  }
  lockWrites();
}
async function reload(explicit = true) {
  if (!state.user) return;
  const generation = state.generation;
  if (state.user.role === "customer") {
    const sessions = await request("/api/sessions");
    if (generation !== state.generation) return;
    renderSessions(sessions);
    if (!state.sid && sessions.length) state.sid = sessions[0].id;
    await loadSession();
  } else if (state.user.role === "staff") { await loadQueue(); await loadTicket(); }
  else if (state.user.role === "operator") {
    const calls = await request("/api/unknown-calls");
    if (generation !== state.generation) return;
    $("unknown-calls").replaceChildren();
    for (const call of calls) $("unknown-calls").append(sourceCard(call.id, call));
    if (!calls.length) $("unknown-calls").append(node("p", "当前无未知调用。", "muted"));
  }
  if (explicit) state.needsReload = false;
  lockWrites();
}
$("login-form").addEventListener("submit", guarded(async () => {
  state.token = $("token").value.trim();
  $("token").value = "";
  try {
    const user = await request("/api/me");
    state.user = user;
    state.generation += 1;
    $("identity").textContent = `${user.user_id} · 角色 ${user.role}`;
    $("mode").textContent = user.test_mode ? "TEST · 脚本演示，不是LLM" : "本地模型 · 非真实支付系统";
    $("mode").className = user.test_mode ? "test-badge" : "";
    $("login-panel").hidden = true;
    $("account-tools").hidden = false;
    for (const role of ["customer", "staff", "operator"]) $(role).hidden = user.role !== role;
    await reload();
    status("已登录。轮询只读取状态，不触发模型调用。");
  } catch (error) {
    if (!state.user) state.token = "";
    throw error;
  }
}));
$("logout").addEventListener("click", guarded(async () => {
  if (state.pending || state.busy) throw new Error("尚有未核实操作，请先使用原 action_id 获取回执再退出。");
  state.token = ""; state.user = null; state.sid = null; state.view = null; state.ticket = null;
  state.generation += 1; state.needsReload = false; state.proposalActions.clear();
  for (const id of ["customer", "staff", "operator", "account-tools", "last-operation"]) $(id).hidden = true;
  for (const id of ["messages", "run-traces", "evidence", "proposals", "applications", "sessions", "queue", "ticket", "operation-info", "metrics", "unknown-calls"]) $(id).replaceChildren();
  for (const id of ["message", "call-id", "call-tokens", "reconcile-note"]) $(id).value = "";
  $("identity").textContent = "仅限本地授权用户";
  $("mode").textContent = "尚未认证"; $("mode").className = "";
  $("login-panel").hidden = false;
  status("本页身份已清除。");
}));
$("reload").addEventListener("click", guarded(async () => { await reload(); status("已重新加载来源版本，请重新核对方案。"); }));
$("retry").addEventListener("click", guarded(async () => { if (state.pending && !state.busy) await sendOperation(state.pending); }));
$("new-session").addEventListener("click", guarded(() => mutate("/api/sessions", {})));
$("message-form").addEventListener("submit", guarded(async () => {
  if (!state.view || state.view.session.id !== state.sid) throw new Error("请先选择会话。");
  const value = $("message").value.trim();
  if (!value) throw new Error("请输入查询或售后需求。");
  await mutate(`/api/sessions/${state.sid}/messages`, {expected_version: state.view.session.version, text: value});
}));
$("cancel").addEventListener("click", guarded(async () => {
  if (!state.view) throw new Error("请先选择会话。");
  await mutate(`/api/sessions/${state.sid}/cancel`, {expected_version: state.view.session.version});
}));
$("reconcile-form").addEventListener("submit", guarded(async () => {
  const value = $("call-tokens").value.trim(), tokens = value === "" ? null : Number(value);
  if (tokens !== null && (!Number.isSafeInteger(tokens) || tokens < 0 || tokens > 10000000)) throw new Error("tokens必须是范围内整数或留空。");
  const call = $("call-id").value.trim(), note = $("reconcile-note").value.trim();
  if (!call || !note) throw new Error("必须提供call_id及核实依据。");
  await mutate("/api/reconcile", {call_id: call, tokens, note, outcome: "no_usable_result"});
}));
let polling = false;
setInterval(async () => {
  if (polling || !state.user || state.busy || state.pending || state.needsReload || document.hidden) return;
  polling = true;
  try {
    if (state.user.role === "customer" && state.sid) await loadSession();
    // Queue polling never loads unclaimed details or overwrites an in-progress review note.
    if (state.user.role === "staff") await loadQueue();
  } catch (_) { status("状态读取失败；未自动提交或重试任何写操作。", true); }
  finally { polling = false; }
}, 2500);
window.addEventListener("beforeunload", (event) => {
  if (state.busy || state.pending) { event.preventDefault(); event.returnValue = ""; }
});
