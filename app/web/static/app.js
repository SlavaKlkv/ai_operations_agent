const state = { currentRun: null, health: null };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function headers() {
  return { "Content-Type": "application/json" };
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* response is not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 4200);
}

function navigate(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  const titles = { investigate: "Новое расследование", history: "История", sources: "Источники данных", settings: "Настройки" };
  $("#view-title").textContent = titles[name];
  if (name === "history") loadHistory();
  if (name === "sources") loadSources();
}

function statusLabel(status) {
  return ({ awaiting_approval: "Ожидает решения", completed: "Завершено", failed: "Ошибка", running: "Выполняется" })[status] || status;
}

async function checkHealth() {
  const pill = $("#health-pill");
  try {
    state.health = await api("/health");
    pill.className = "health-pill ok";
    pill.querySelector("b").textContent = "Система готова";
    $("#storage-state").textContent = `${state.health.storage_backend} · ${state.health.checkpointer}`;
  } catch (error) {
    pill.className = "health-pill error";
    pill.querySelector("b").textContent = "Нет соединения";
    $("#storage-state").textContent = "Сервис недоступен";
  }
}

function renderRun(run) {
  state.currentRun = run;
  $("#run-panel").classList.add("hidden");
  $("#result").classList.remove("hidden");
  $("#result-summary").textContent = run.analysis?.summary || run.final_result || "Расследование завершено без вывода.";
  $("#confidence").textContent = run.analysis ? `${Math.round(run.analysis.confidence * 100)}% confidence` : statusLabel(run.status);
  $("#symptoms").innerHTML = (run.analysis?.symptoms || ["Подтверждённые симптомы не найдены."]).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("#actions").innerHTML = (run.analysis?.recommended_actions || ["Уточните описание и повторите расследование."]).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const evidence = run.analysis?.evidence || [];
  $("#evidence-count").textContent = evidence.length;
  $("#evidence-list").innerHTML = evidence.map((item) => `<div class="evidence-item"><code>${escapeHtml(item.reference)}</code><p>${escapeHtml(item.summary)}</p></div>`).join("") || '<p class="privacy-note">Доказательства не собраны.</p>';
  const approval = $("#approval");
  if (run.pending_approval) {
    approval.classList.remove("hidden");
    $("#approval-rationale").textContent = run.pending_approval.rationale;
    const args = run.pending_approval.arguments;
    $("#issue-preview").textContent = `${args.title || ""}\n\n${args.body || JSON.stringify(args, null, 2)}`;
  } else {
    approval.classList.add("hidden");
  }
}

async function startInvestigation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const panel = $("#run-panel");
  button.disabled = true;
  $("#result").classList.add("hidden");
  panel.classList.remove("hidden");
  $("#run-id").textContent = "new run";
  try {
    const payload = { task: $("#task").value.trim() };
    const service = $("#service").value.trim();
    if (service) payload.target_service = service;
    const run = await api("/runs", { method: "POST", body: JSON.stringify(payload) });
    $("#run-id").textContent = run.id;
    renderRun(run);
    toast(run.status === "awaiting_approval" ? "Расследование ожидает вашего решения" : "Расследование завершено");
  } catch (error) {
    panel.classList.add("hidden");
    toast(`Не удалось начать расследование: ${error.message}`, true);
  } finally { button.disabled = false; }
}

async function decide(approved) {
  if (!state.currentRun) return;
  const buttons = [$("#approve"), $("#reject")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const run = await api(`/runs/${state.currentRun.id}/approval`, {
      method: "POST",
      body: JSON.stringify({ approved, note: $("#approval-note").value.trim() }),
    });
    renderRun(run);
    toast(approved ? "Действие подтверждено" : "Действие отклонено");
  } catch (error) { toast(`Решение не сохранено: ${error.message}`, true); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

async function loadHistory() {
  const target = $("#history-list");
  target.innerHTML = '<div class="empty-card">Загружаем историю…</div>';
  try {
    const runs = await api("/runs");
    target.innerHTML = runs.map((run) => `<article class="history-card" data-run="${run.id}"><div><h3>${escapeHtml(run.task)}</h3><p>${new Date(run.created_at).toLocaleString("ru-RU")} · ${run.tool_call_count} вызовов</p></div><span class="state-tag">${statusLabel(run.status)}</span></article>`).join("") || '<div class="empty-card">Расследований пока нет.</div>';
    target.querySelectorAll("[data-run]").forEach((card) => card.addEventListener("click", async () => {
      try { renderRun(await api(`/runs/${card.dataset.run}`)); navigate("investigate"); } catch (error) { toast(error.message, true); }
    }));
  } catch (error) { target.innerHTML = `<div class="empty-card">${escapeHtml(error.message)}</div>`; }
}

async function loadSources() {
  const target = $("#source-grid");
  try {
    const response = await fetch("/mcp/servers", { headers: headers() });
    const sources = await response.json();
    const items = Array.isArray(sources) ? sources : (sources.servers || []);
    target.innerHTML = items.map((source) => `<article class="source-card"><h3>${escapeHtml(source.name || "Источник")}</h3><p>${escapeHtml(source.error || `${source.tool_count || 0} инструментов доступно`)}</p><footer><span>${source.required ? "Обязательный" : "Дополнительный"}</span><span class="${source.connected ? "connected" : ""}">${source.connected ? "Подключён" : "Недоступен"}</span></footer></article>`).join("") || '<div class="empty-card">Источники ещё не настроены.</div>';
  } catch (error) { target.innerHTML = `<div class="empty-card">${escapeHtml(error.message)}</div>`; }
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

$$("[data-view]").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.view)));
$("#investigation-form").addEventListener("submit", startInvestigation);
$("#approve").addEventListener("click", () => decide(true));
$("#reject").addEventListener("click", () => decide(false));
$("#refresh-history").addEventListener("click", loadHistory);
checkHealth();
