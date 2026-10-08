const state = { currentRun: null, health: null, setup: null };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let downloadWatching = false;
let setupReturnFocus = null;

function setSetupOpen(open) {
  const overlay = $("#setup-overlay");
  if (open && overlay.classList.contains("hidden")) {
    setupReturnFocus = document.activeElement;
    overlay.classList.remove("hidden");
    $(".shell").inert = true;
    $("#setup-title").focus();
  } else if (!open) {
    overlay.classList.add("hidden");
    $(".shell").inert = false;
    if (setupReturnFocus?.isConnected && setupReturnFocus !== document.body) {
      setupReturnFocus.focus();
    } else {
      $("#task").focus();
    }
  }
}

$("#setup-overlay").addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const focusable = $$("#setup-overlay a[href], #setup-overlay button:not(:disabled), #setup-overlay input:not(:disabled), #setup-overlay select:not(:disabled)")
    .filter((element) => element.getClientRects().length);
  if (!focusable.length) return;
  if (event.shiftKey && (document.activeElement === focusable[0] || document.activeElement === $("#setup-title"))) {
    event.preventDefault(); focusable.at(-1).focus();
  } else if (!event.shiftKey && document.activeElement === $("#setup-title")) {
    event.preventDefault(); focusable[0].focus();
  } else if (!event.shiftKey && document.activeElement === focusable.at(-1)) {
    event.preventDefault(); focusable[0].focus();
  }
});
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
  if (name === "settings") loadSetup(false);
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

function setupCheck(title, check) {
  return `<article class="setup-check${check.ready ? " ready" : ""}"><span>${check.ready ? "✓" : "○"}</span><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(check.status)}</p>${check.action ? `<p class="action">${escapeHtml(check.action)}</p>` : ""}</div></article>`;
}

function renderSetup(data, showOverlay = true) {
  state.setup = data;
  const sourceChecks = data.sources.map((source) => setupCheck(source.name, {
    ready: source.ready,
    status: source.detail,
    action: source.ready ? null : (source.required ? "Проверьте обязательный источник." : "Можно настроить позже."),
  })).join("");
  $("#setup-checks").innerHTML = [
    setupCheck("Ollama", data.ollama),
    setupCheck("Локальное хранилище", data.storage),
    setupCheck("GitHub", data.github),
    sourceChecks,
  ].join("");

  const installed = new Set(data.model.installed_models);
  $("#profile-grid").innerHTML = data.model.profiles.map((profile) => {
    const isInstalled = installed.has(profile.model_name);
    const active = data.model.selection.profile === profile.slug;
    const action = !data.ollama.ready ? "Ollama недоступен" : (!isInstalled ? "Скачать" : (active ? "Выбрана" : "Выбрать"));
    return `<button class="profile-card${active ? " active" : ""}" type="button" ${!data.ollama.ready ? "disabled" : (isInstalled ? `data-profile="${profile.slug}"` : `data-download="${profile.slug}"`)}><header><h4>${escapeHtml(profile.label)}</h4><code>${escapeHtml(profile.model_name)}</code></header><p>${escapeHtml(profile.description)}</p><footer><span>≈ ${profile.download_size_gb} ГБ</span><span>${action}</span></footer></button>`;
  }).join("");
  $("#installed-models").innerHTML = data.model.installed_models.map((name) => `<option value="${escapeHtml(name)}"></option>`).join("");
  $("#model-caption").textContent = data.model.installed
    ? `Активна ${data.model.selection.model_name}${data.model.selection.verified ? " · проверенный профиль" : " · не проверена"}`
    : `Модель ${data.model.selection.model_name} не установлена`;
  $("#active-model").textContent = `Модель для новых расследований: ${data.model.selection.model_name}${data.model.selection.verified ? "" : " · Не проверена"}`;

  const checks = [data.ollama.ready, data.model.installed, data.storage.ready, data.github.ready, ...data.sources.filter((item) => item.required).map((item) => item.ready)];
  const readyCount = checks.filter(Boolean).length;
  $("#setup-progress").textContent = `${readyCount} из ${checks.length} готово`;
  $("#finish-setup").disabled = !data.ready;
  if (showOverlay) setSetupOpen(true);
  $$("[data-profile]").forEach((button) => button.addEventListener("click", () => selectModel({ profile: button.dataset.profile })));
  $$("[data-download]").forEach((button) => button.addEventListener("click", () => startModelDownload(button.dataset.download)));
}

async function loadSetup(showOverlay = true) {
  try {
    const data = await api("/setup");
    renderSetup(data, showOverlay && sessionStorage.getItem("aoa-demo-continued") !== "true");
    await loadDownload();
    await loadGitHub();
  } catch (error) {
    $("#active-model").textContent = "Настройки недоступны";
    if (showOverlay) toast(`Не удалось проверить настройку: ${error.message}`, true);
  }
}

function renderDownload(download) {
  const panel = $("#download-progress");
  panel.classList.remove("hidden");
  const label = download.state === "complete" ? "Загрузка завершена" : (download.error || download.status);
  const progress = download.total > 0
    ? `<progress value="${download.completed}" max="${download.total}"></progress><span>${Math.min(100, Math.round(download.completed / download.total * 100))}%</span>`
    : "<progress></progress>";
  panel.innerHTML = `<div>${escapeHtml(download.model)} · ${escapeHtml(label)}</div>${download.state === "running" ? `${progress}<button class="secondary" id="cancel-download" type="button">Отменить загрузку</button>` : ""}`;
  if (download.state === "running") $("#cancel-download").addEventListener("click", async () => {
    try { await api("/setup/model/download", { method: "DELETE" }); await loadDownload(); }
    catch (error) { toast(error.message, true); }
  });
}

async function loadDownload() {
  try {
    const download = await api("/setup/model/download");
    renderDownload(download);
    if (download.state === "running" && !downloadWatching) watchDownload();
  } catch (_) { $("#download-progress").classList.add("hidden"); }
}

async function startModelDownload(profile) {
  try {
    const download = await api("/setup/model/download", { method: "POST", body: JSON.stringify({ profile }) });
    renderDownload(download);
    watchDownload();
  } catch (error) { toast(error.message, true); }
}

async function watchDownload() {
  if (downloadWatching) return;
  downloadWatching = true;
  try {
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const download = await api("/setup/model/download");
      renderDownload(download);
      if (download.state === "running") continue;
      if (download.state === "complete") {
        const profile = state.setup?.model.profiles.find((item) => item.model_name === download.model);
        if (profile) await selectModel({ profile: profile.slug });
      } else if (download.state === "failed") toast(download.error || "Загрузка модели не удалась", true);
      break;
    }
  } catch (error) { toast(error.message, true); }
  finally { downloadWatching = false; }
}

async function loadGitHub() {
  const controls = $("#github-controls");
  try {
    const status = await api("/github/status");
    if (!status.configured) {
      $("#github-caption").textContent = "Общая GitHub App ещё не настроена в этой сборке.";
      controls.innerHTML = "";
      $("#github-preview").innerHTML = "";
      return;
    }
    if (!status.connected) {
      $("#github-caption").textContent = status.error || "Подключите аккаунт без PAT через GitHub Device Flow.";
      controls.innerHTML = '<button class="secondary" id="github-connect" type="button">Подключить GitHub</button>';
      $("#github-preview").innerHTML = "";
      $("#github-connect").addEventListener("click", startGitHubConnection);
      return;
    }
    $("#github-caption").textContent = `Аккаунт ${status.login} подключён${status.selected ? ` · ${status.selected.full_name}` : " · выберите репозиторий"}`;
    controls.innerHTML = `<button class="secondary" id="github-disconnect" type="button">Отключить</button>${status.install_url ? `<a class="secondary" href="${escapeHtml(status.install_url)}" target="_blank" rel="noopener noreferrer">Установить GitHub App</a>` : ""}<select id="github-installation" aria-label="Установка GitHub App"><option value="">Выберите установку</option></select><select id="github-repository" aria-label="Репозиторий"><option value="">Выберите репозиторий</option></select>`;
    $("#github-disconnect").addEventListener("click", async () => {
      try { await api("/github", { method: "DELETE" }); await loadSetup(true); }
      catch (error) { toast(error.message, true); }
    });
    const installations = await api("/github/installations");
    $("#github-installation").innerHTML += installations.map((item) => `<option value="${item.id}">${escapeHtml(item.account)}</option>`).join("");
    $("#github-installation").addEventListener("change", async (event) => {
      const id = Number(event.target.value);
      const select = $("#github-repository");
      select.innerHTML = '<option value="">Выберите репозиторий</option>';
      if (!id) return;
      try {
        const repositories = await api(`/github/installations/${id}/repositories`);
        select.innerHTML += repositories.map((item) => `<option value="${item.id}">${escapeHtml(item.full_name)}</option>`).join("");
        if (status.selected?.installation_id === id) select.value = String(status.selected.id);
      } catch (error) { toast(error.message, true); }
    });
    $("#github-repository").addEventListener("change", async (event) => {
      if (!event.target.value) return;
      try {
        await api("/github/repository", { method: "PUT", body: JSON.stringify({ installation_id: Number($("#github-installation").value), repository_id: Number(event.target.value) }) });
        await loadSetup(true);
      } catch (error) { toast(error.message, true); }
    });
    if (status.selected) {
      $("#github-installation").value = String(status.selected.installation_id);
      $("#github-installation").dispatchEvent(new Event("change"));
      try {
        const preview = await api("/github/preview");
        const groups = [
          ["Коммиты", preview.commits.map((item) => `${item.sha} · ${item.message}`)],
          ["Pull requests", preview.pull_requests.map((item) => `#${item.number} · ${item.title}`)],
          ["Deployments", preview.deployments.map((item) => `${item.environment} · ${item.sha}`)],
        ];
        $("#github-preview").innerHTML = groups.map(([title, items]) => `<div class="github-preview-group"><h4>${escapeHtml(title)}</h4><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>Нет данных</li>"}</ul></div>`).join("");
      } catch (error) { $("#github-preview").textContent = `Чтение GitHub недоступно: ${error.message}`; }
    } else {
      $("#github-preview").innerHTML = "";
    }
  } catch (error) { $("#github-caption").textContent = error.message; controls.innerHTML = ""; }
}

async function startGitHubConnection() {
  try {
    const grant = await api("/github/device/start", { method: "POST" });
    $("#github-caption").textContent = "Откройте GitHub, введите код и подтвердите доступ.";
    $("#github-controls").innerHTML = `<span class="device-code">${escapeHtml(grant.user_code)}</span><a class="secondary" href="${escapeHtml(grant.verification_uri)}" target="_blank" rel="noopener noreferrer">Открыть GitHub</a><button class="secondary" id="github-cancel" type="button">Отмена</button>`;
    let active = true;
    $("#github-cancel").addEventListener("click", async () => {
      active = false;
      try { await api(`/github/device/${encodeURIComponent(grant.flow_id)}`, { method: "DELETE" }); }
      catch (error) { toast(error.message, true); }
      await loadGitHub();
    });
    while (active) {
      await new Promise((resolve) => setTimeout(resolve, grant.interval * 1000));
      if (!active) break;
      const result = await api(`/github/device/${encodeURIComponent(grant.flow_id)}/poll`, { method: "POST" });
      if (result.state === "pending") { grant.interval = result.retry_after || grant.interval; continue; }
      if (result.state === "connected") { toast(`GitHub: ${result.login} подключён`); await loadSetup(true); break; }
      toast(result.state === "denied" ? "Подключение отклонено" : "Код истёк. Начните заново.", true);
      await loadGitHub();
      break;
    }
  } catch (error) { toast(error.message, true); await loadGitHub(); }
}

async function selectModel(payload) {
  $$("[data-profile]").forEach((button) => { button.disabled = true; });
  try {
    await api("/setup/model", { method: "PUT", body: JSON.stringify(payload) });
    toast(payload.profile === "custom" ? "Совместимая модель выбрана с пометкой «Не проверена»" : "Профиль модели изменён");
    await loadSetup(true);
  } catch (error) { toast(error.message, true); }
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
$("#open-setup").addEventListener("click", () => { sessionStorage.removeItem("aoa-demo-continued"); loadSetup(true); });
$("#continue-demo").addEventListener("click", () => { sessionStorage.setItem("aoa-demo-continued", "true"); setSetupOpen(false); });
$("#finish-setup").addEventListener("click", () => { setSetupOpen(false); toast("Настройка завершена"); });
$("#custom-model-form").addEventListener("submit", (event) => { event.preventDefault(); selectModel({ profile: "custom", model_name: $("#custom-model-name").value.trim() }); });
checkHealth();
loadSetup(true);
