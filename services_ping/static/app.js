(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const dom = {
    backendBanner: $("backend-banner"), connectivity: $("connectivity-banner"),
    connectivityTitle: $("connectivity-title"), connectivityMessage: $("connectivity-message"),
    connectivityLatency: $("connectivity-latency"), connectivityTime: $("connectivity-time"),
    references: $("references"), groups: $("target-groups"), empty: $("empty-targets"),
    summary: $("targets-summary"), pauseNote: $("pause-note"), lastUpdate: $("last-update"),
    pause: $("pause-toggle"), check: $("manual-check"), events: $("events"),
    targetDialog: $("target-dialog"), targetForm: $("target-form"), targetError: $("target-form-error"),
    settingsDialog: $("settings-dialog"), settingsForm: $("settings-form"), settingsError: $("settings-form-error"),
    refEditor: $("connectivity-editor"), historyDialog: $("history-dialog"), chart: $("history-chart"),
    historyList: $("history-list"), historyTitle: $("history-title"), historySubtitle: $("history-subtitle"),
    importDialog: $("import-dialog"), importFile: $("import-file"), importSummary: $("import-summary"),
    importError: $("import-error"), importConfirm: $("confirm-import"), importApply: $("apply-import"),
    notification: $("notification-button"), toastRegion: $("toast-region")
  };
  const app = { state: null, config: null, csrf: null, pollTimer: 0, polling: false, importConfig: null, previousInternet: null };
  const statusText = {
    checking: "Проверяется", online: "Онлайн", partial: "Частичный доступ", offline: "Недоступен по проверкам",
    pending: "Ожидает", up: "Работает", reachable: "Доступен", down: "Недоступен", blocked: "Доступ есть, запрет",
    rate_limited: "Ограничение запросов", error: "Ошибка", unsupported: "Не поддерживается", no_internet: "Нет интернета", disabled: "Отключена"
  };
  const titlePrefix = { online: "Есть интернет", partial: "Частичный доступ", offline: "Нет интернета", checking: "Проверяем интернет" };
  function setPageTitle(state) { const prefix = titlePrefix[state]; document.title = prefix ? `${prefix} Services Ping` : "Services Ping"; }
  class ApiError extends Error { constructor(message, status = 0) { super(message); this.status = status; } }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(text, className, handler, label) {
    const node = element("button", className, text); node.type = "button";
    if (label) node.setAttribute("aria-label", label);
    node.addEventListener("click", handler); return node;
  }
  function setError(node, message) { node.textContent = message || ""; node.hidden = !message; }
  function toast(message, isError = false) {
    const node = element("div", `toast${isError ? " error" : ""}`, message);
    dom.toastRegion.append(node); window.setTimeout(() => node.remove(), 5500);
  }
  function cleanError(error) { return error instanceof Error && error.message ? error.message : "Не удалось выполнить запрос"; }
  function localTime(value) {
    if (!value) return "ещё нет результата";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? "время неизвестно" : date.toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" });
  }
  function latency(value) { return value != null && Number.isFinite(Number(value)) ? `${Math.round(Number(value))} мс` : "—"; }
  function badge(status) { return element("span", `badge ${status || "pending"}`, statusText[status] || status || "—"); }
  function layer(kind) { return ({ http: "HTTP: DNS, TCP, TLS и заголовки", tcp: "TCP: DNS и соединение", icmp: "ICMP: RTT" })[kind] || "Сетевая проверка"; }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function safeId(prefix) { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`; }

  async function requestJson(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal });
      const body = await response.json();
      return { response, body };
    } finally { window.clearTimeout(timer); }
  }
  async function session() {
    const { response, body } = await requestJson("/api/session", { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!response.ok || !body.csrf_token) throw new ApiError(body.error || "Не удалось получить токен сессии", response.status);
    app.csrf = body.csrf_token;
  }
  async function api(path, options = {}, retried = false) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {}); headers.set("Accept", "application/json");
    if (method !== "GET" && method !== "HEAD") { if (!app.csrf) await session(); headers.set("X-CSRF-Token", app.csrf); }
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    let response, body;
    try { ({ response, body } = await requestJson(path, { method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: "same-origin" })); }
    catch (_) { throw new ApiError("Локальный монитор недоступен. Проверьте, что процесс запущен."); }
    if (!response.ok) {
      if (response.status === 403 && method !== "GET" && !retried) { await session(); return api(path, options, true); }
      throw new ApiError(body.error || `Ошибка сервера (${response.status})`, response.status);
    }
    return body;
  }
  async function loadConfig() { app.config = await api("/api/config"); return app.config; }

  function renderInternet(internet) {
    const info = internet || { status: "checking", message: "Ожидаем состояние монитора" };
    const state = info.status || "checking";
    setPageTitle(state);
    dom.connectivity.className = `connectivity-card is-${state}`;
    dom.connectivityTitle.textContent = ({ online: "Интернет доступен", partial: "Частичный доступ к интернету", offline: "Интернет недоступен по проверкам", checking: "Проверяем интернет…" })[state] || "Проверяем интернет…";
    dom.connectivityMessage.textContent = info.message || (state === "offline" ? "Это может быть DNS, фильтрация или captive portal, а не физическое отключение." : "Опрашиваем независимые опорные адреса");
    dom.connectivityLatency.textContent = info.latency_ms == null ? "—" : `Лучшая проверка: ${latency(info.latency_ms)}`;
    dom.connectivityTime.textContent = localTime(info.checked_at);
  }
  function renderReferences(references) {
    dom.references.replaceChildren();
    for (const reference of references || []) {
      const card = element("article", "reference-card");
      const head = element("div", "reference-card-head"); head.append(element("h3", "", reference.name || "Без названия"), badge(reference.status || "checking"));
      const address = element("span", "reference-address", reference.url || reference.address || ""); address.title = address.textContent; address.hidden = !address.textContent;
      const footer = element("div", "reference-footer"); footer.append(element("span", "", latency(reference.latency_ms)), element("span", "", reference.http_status ? `HTTP ${reference.http_status}` : localTime(reference.checked_at)));
      card.append(head, address, footer);
      if (reference.message) card.append(element("div", "reference-message muted", reference.message));
      dom.references.append(card);
    }
    if (!references || !references.length) dom.references.append(element("p", "muted", "Опорные адреса ещё не загружены."));
  }
  function targetReading(target) {
    const measured = target.stale && target.last_result ? target.last_result : target;
    return { latency: measured && measured.latency_ms, checkedAt: measured && (measured.checked_at || measured.timestamp), message: target.message || (measured && measured.message), httpStatus: measured && measured.http_status };
  }
  function targetCard(target) {
    const card = element("article", `target-card${target.stale ? " is-stale" : ""}`); card.dataset.targetId = target.id || "";
    const top = element("div", "target-card-top"); const titleRow = element("div", "target-title-row");
    titleRow.append(element("h3", "target-title", target.name || "Без названия"), element("span", "target-kind", (target.kind || "?").toUpperCase()));
    top.append(titleRow, badge(target.status || "pending"));
    const address = element("p", "target-address", target.address || "Адрес не указан"); address.title = address.textContent;
    const reading = targetReading(target); const displayLatency = target.enabled === false ? null : reading.latency;
    const meter = element("div", "target-reading"); meter.append(element("span", "target-latency", latency(displayLatency)), element("span", "target-layer", layer(target.kind)));
    const message = element("p", "target-message", reading.message || (target.in_flight ? "Проверка выполняется…" : "Ещё не проверен"));
    const footer = element("div", "target-footer");
    const time = element("span", "", target.stale ? `Устаревший результат: ${localTime(reading.checkedAt)}` : (target.in_flight ? "Проверка выполняется…" : localTime(reading.checkedAt)));
    const actions = element("div", "card-actions");
    actions.append(button("История", "text-button", () => showHistory(target), `История: ${target.name || "цель"}`), button("Изм.", "text-button", () => openTarget(target.id), `Изменить: ${target.name || "цель"}`), button("Удал.", "text-button", () => deleteTarget(target), `Удалить: ${target.name || "цель"}`));
    footer.append(time, actions); card.append(top, address, meter, message, footer); return card;
  }
  function renderTargets(targets) {
    dom.groups.replaceChildren(); const list = targets || [];
    dom.empty.hidden = list.length !== 0; dom.summary.textContent = list.length ? `Всего целей: ${list.length}` : "Добавьте первую цель для наблюдения";
    const byGroup = new Map();
    list.forEach((target) => { const group = (target.group || "Без группы").trim() || "Без группы"; if (!byGroup.has(group)) byGroup.set(group, []); byGroup.get(group).push(target); });
    for (const [group, entries] of byGroup) {
      const wrap = element("section", "target-group"); const heading = element("h3", "group-heading", group); const grid = element("div", "target-grid");
      entries.forEach((target) => grid.append(targetCard(target))); wrap.append(heading, grid); dom.groups.append(wrap);
    }
  }
  function renderEvents(events) {
    dom.events.replaceChildren();
    if (!events || !events.length) { dom.events.append(element("li", "empty-list", "Изменений пока нет.")); return; }
    for (const event of events) {
      const item = element("li"); item.append(element("time", "event-time", localTime(event.timestamp)), element("span", "", event.message || "Состояние изменилось"), element("span", "event-transition", `${statusText[event.previous] || event.previous || "—"} → ${statusText[event.current] || event.current || "—"}`)); dom.events.append(item);
    }
  }
  function updateControls() {
    const paused = Boolean(app.state && app.state.paused); dom.pause.textContent = paused ? "Продолжить" : "Пауза"; dom.pause.setAttribute("aria-pressed", String(paused)); dom.pauseNote.hidden = !paused;
  }
  function renderState(state) {
    app.state = state; dom.backendBanner.hidden = true;
    renderInternet(state.internet); renderReferences((state.internet && state.internet.references) || []); renderTargets(state.targets || []); updateControls();
    dom.lastUpdate.textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    notifyOnInternetChange(state.internet && state.internet.status);
  }
  async function refreshEvents() { try { renderEvents((await api("/api/events?limit=50")).events); } catch (error) { toast(cleanError(error), true); } }

  function schedulePoll() { window.clearTimeout(app.pollTimer); app.pollTimer = window.setTimeout(pollState, document.hidden ? 5000 : 1000); }
  async function pollState() {
    if (app.polling) return; app.polling = true;
    try { renderState(await api("/api/state")); }
    catch (error) {
      if (!(error instanceof ApiError) || !error.status) { dom.backendBanner.hidden = false; dom.lastUpdate.textContent = "Нет связи с локальным монитором"; setPageTitle(""); }
      else toast(cleanError(error), true);
    } finally { app.polling = false; schedulePoll(); }
  }
  async function manualCheck() { try { await api("/api/check", { method: "POST", body: {} }); toast("Запущен внеочередной цикл проверки."); await pollState(); } catch (error) { toast(cleanError(error), true); } }
  async function togglePause() { try { const response = await api("/api/pause", { method: "POST", body: { paused: !(app.state && app.state.paused) } }); if (app.state) { app.state.paused = response.paused; updateControls(); } toast(response.paused ? "Автопроверки поставлены на паузу." : "Автопроверки возобновлены."); await pollState(); } catch (error) { toast(cleanError(error), true); } }

  function updateKindHelp() {
    const kind = $("target-kind").value; const input = $("target-address"); const help = $("address-help"); const statuses = $("target-statuses");
    if (kind === "http") { input.placeholder = "https://example.com/"; help.textContent = "HTTP/HTTPS адрес без логина и пароля"; statuses.disabled = false; }
    else if (kind === "tcp") { input.placeholder = "host.example:443"; help.textContent = "Имя хоста и порт; IPv6 заключите в [скобки]"; statuses.disabled = true; statuses.value = ""; }
    else { input.placeholder = "vpn.example или 192.0.2.1"; help.textContent = "Имя хоста или IP; системный ping должен быть доступен"; statuses.disabled = true; statuses.value = ""; }
  }
  function parseStatuses(value) {
    const raw = value.trim(); if (!raw) return [];
    const numbers = raw.split(",").map((part) => Number(part.trim()));
    if (numbers.some((n) => !Number.isInteger(n) || n < 100 || n > 599)) throw new Error("Укажите HTTP-статусы от 100 до 599 через запятую.");
    return [...new Set(numbers)];
  }
  function targetFromForm() {
    const form = dom.targetForm; const kind = $("target-kind").value; const name = $("target-name").value.trim(); const address = $("target-address").value.trim();
    if (!name || !address) throw new Error("Заполните название и адрес.");
    if (kind === "http") { let url; try { url = new URL(address); } catch (_) { throw new Error("Укажите корректный HTTP/HTTPS адрес."); } if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw new Error("Допускается только HTTP/HTTPS адрес без логина и пароля."); }
    if (kind === "tcp" && !address.includes(":")) throw new Error("Для TCP укажите адрес и порт, например host:443.");
    return { id: $("target-id").value || safeId("target"), name, group: $("target-group").value.trim(), kind, address, enabled: $("target-enabled").checked, interval_s: Number($("target-interval").value), timeout_s: Number($("target-timeout").value), expected_statuses: kind === "http" ? parseStatuses($("target-statuses").value) : [] };
  }
  async function openTarget(id = null) {
    setError(dom.targetError, ""); let source;
    try { await loadConfig(); source = id ? app.config.targets.find((target) => target.id === id) : null; if (id && !source) throw new Error("Цель уже была удалена."); }
    catch (error) { toast(cleanError(error), true); return; }
    const defaults = app.config.settings || {}; $("target-dialog-title").textContent = source ? "Изменить цель" : "Новая цель";
    $("target-id").value = source ? source.id : ""; $("target-name").value = source ? source.name || "" : ""; $("target-group").value = source ? source.group || "" : "";
    $("target-kind").value = source ? source.kind : "http"; $("target-address").value = source ? source.address || "" : ""; $("target-enabled").checked = source ? source.enabled !== false : true;
    $("target-interval").value = source ? source.interval_s : defaults.service_interval_s || 30; $("target-timeout").value = source ? source.timeout_s : defaults.timeout_s || 2;
    $("target-statuses").value = source && source.expected_statuses ? source.expected_statuses.join(", ") : ""; updateKindHelp(); dom.targetDialog.showModal(); $("target-name").focus();
  }
  async function saveTarget(event) {
    event.preventDefault(); setError(dom.targetError, ""); let target;
    try { target = targetFromForm(); } catch (error) { setError(dom.targetError, cleanError(error)); return; }
    let latest;
    try { latest = await loadConfig(); } catch (error) { setError(dom.targetError, cleanError(error)); return; }
    const existing = latest.targets.findIndex((item) => item.id === target.id); const next = clone(latest); if (existing >= 0) next.targets[existing] = target; else next.targets.push(target);
    try { app.config = await api("/api/config", { method: "PUT", body: next }); dom.targetDialog.close(); toast(existing >= 0 ? "Цель обновлена." : "Цель добавлена."); await pollState(); }
    catch (error) { setError(dom.targetError, cleanError(error)); }
  }
  async function deleteTarget(target) {
    if (!window.confirm(`Удалить цель «${target.name || "без названия"}» и её историю?`)) return;
    try { await loadConfig(); const next = clone(app.config); next.targets = next.targets.filter((item) => item.id !== target.id); app.config = await api("/api/config", { method: "PUT", body: next }); toast("Цель удалена."); await pollState(); }
    catch (error) { toast(cleanError(error), true); }
  }

  function referenceRow(reference = {}) {
    const row = element("div", "reference-edit-row");
    const fields = [["Название", "name", reference.name || ""], ["HTTPS-адрес", "url", reference.url || ""], ["Статусы", "statuses", (reference.expected_statuses || []).join(", ")]];
    for (const [labelText, field, value] of fields) { const label = element("label", "", labelText); const input = document.createElement("input"); input.dataset.field = field; input.value = value; input.required = true; input.autocomplete = "off"; if (field === "statuses") { input.required = false; input.placeholder = "200, 204"; input.inputMode = "numeric"; } label.append(input); row.append(label); }
    row.dataset.id = reference.id || safeId("net"); row.append(button("×", "icon-button", () => row.remove(), "Удалить опорный адрес")); return row;
  }
  function populateSettings(config) {
    const settings = config.settings || {}; for (const field of ["internet_interval_s", "service_interval_s", "timeout_s", "concurrency", "history_limit"]) dom.settingsForm.elements[field].value = settings[field] ?? "";
    dom.settingsForm.elements.notifications.checked = Boolean(settings.notifications); dom.refEditor.replaceChildren(); (config.connectivity || []).forEach((reference) => dom.refEditor.append(referenceRow(reference)));
  }
  async function openSettings() { setError(dom.settingsError, ""); try { populateSettings(await loadConfig()); dom.settingsDialog.showModal(); } catch (error) { toast(cleanError(error), true); } }
  function configFromSettings() {
    const next = clone(app.config); const form = dom.settingsForm; next.settings = { internet_interval_s: Number(form.elements.internet_interval_s.value), service_interval_s: Number(form.elements.service_interval_s.value), timeout_s: Number(form.elements.timeout_s.value), concurrency: Number(form.elements.concurrency.value), history_limit: Number(form.elements.history_limit.value), notifications: form.elements.notifications.checked };
    const references = [...dom.refEditor.children].map((row) => { const value = (field) => row.querySelector(`[data-field="${field}"]`).value.trim(); const url = value("url"); let parsed; try { parsed = new URL(url); } catch (_) { throw new Error("Укажите корректный HTTPS-адрес опорной проверки."); } if (parsed.protocol !== "https:") throw new Error("Для опорных проверок разрешён только HTTPS."); const name = value("name"); if (!name) throw new Error("У каждой опорной проверки должно быть название."); return { id: row.dataset.id || safeId("net"), name, url, expected_statuses: parseStatuses(value("statuses")) }; });
    if (references.length < 2) throw new Error("Добавьте не менее двух независимых опорных адресов."); next.connectivity = references; return next;
  }
  async function saveSettings(event) {
    event.preventDefault(); setError(dom.settingsError, ""); let next;
    try { next = configFromSettings(); } catch (error) { setError(dom.settingsError, cleanError(error)); return; }
    try {
      const latest = await loadConfig();
      // Settings owns only these two config sections; keep targets changed elsewhere while the dialog was open.
      next.targets = latest.targets;
      app.config = await api("/api/config", { method: "PUT", body: next }); dom.settingsDialog.close(); toast("Настройки сохранены."); await pollState();
    }
    catch (error) { setError(dom.settingsError, cleanError(error)); }
  }
  async function exportConfig() {
    try {
      const { response, body } = await requestJson("/api/export", { credentials: "same-origin" }); if (!response.ok) throw new ApiError(body.error || "Не удалось экспортировать настройки", response.status);
      const blob = new Blob([JSON.stringify(body, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "services-ping-config.json"; document.body.append(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); toast("Конфигурация сохранена в JSON.");
    } catch (error) { toast(cleanError(error), true); }
  }
  async function selectImport(event) {
    const file = event.target.files && event.target.files[0]; event.target.value = ""; if (!file) return;
    try { const parsed = JSON.parse(await file.text()); if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.targets) || !Array.isArray(parsed.connectivity) || !parsed.settings) throw new Error("Это не конфигурация Services Ping версии 1."); app.importConfig = parsed; dom.importSummary.textContent = `Будут заменены: целей — ${parsed.targets.length}, опорных адресов — ${parsed.connectivity.length}.`; setError(dom.importError, ""); dom.importConfirm.checked = false; dom.importApply.disabled = true; dom.settingsDialog.close(); dom.importDialog.showModal(); }
    catch (error) { toast(`Импорт не выполнен: ${cleanError(error)}`, true); }
  }
  async function applyImport() {
    if (!app.importConfig || !dom.importConfirm.checked) return; setError(dom.importError, ""); dom.importApply.disabled = true;
    try { app.config = await api("/api/config", { method: "PUT", body: app.importConfig }); dom.importDialog.close(); dom.settingsDialog.close(); toast("Импортированная конфигурация применена."); await pollState(); }
    catch (error) { setError(dom.importError, cleanError(error)); dom.importApply.disabled = false; }
  }

  function svgNode(name, attrs = {}) { const node = document.createElementNS("http://www.w3.org/2000/svg", name); for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value)); return node; }
  async function showHistory(target) {
    dom.historyTitle.textContent = `История: ${target.name || "цель"}`; dom.historySubtitle.textContent = target.address || ""; dom.chart.replaceChildren(element("span", "chart-empty", "Загружаем историю…")); dom.historyList.replaceChildren(); dom.historyDialog.showModal();
    try { const data = await api(`/api/history?target_id=${encodeURIComponent(target.id)}&limit=60`); renderHistory(data.samples || []); }
    catch (error) { dom.chart.replaceChildren(element("span", "chart-empty", cleanError(error))); }
  }
  function renderHistory(samples) {
    dom.chart.replaceChildren(); dom.historyList.replaceChildren();
    if (!samples.length) { dom.chart.append(element("span", "chart-empty", "Для этой цели ещё нет измерений.")); return; }
    const points = samples.map((sample, index) => ({ index, value: sample.latency_ms == null ? NaN : Number(sample.latency_ms), sample })).filter((point) => Number.isFinite(point.value));
    if (points.length) {
      const width = 720, height = 210, pad = { l: 42, r: 12, t: 16, b: 31 }, max = Math.max(...points.map((point) => point.value), 1), min = Math.min(...points.map((point) => point.value), 0), range = Math.max(max - min, max * .15, 1);
      const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", role: "img", "aria-label": `График задержки, от ${Math.round(min)} до ${Math.round(max)} миллисекунд` });
      for (let tick = 0; tick < 4; tick++) { const y = pad.t + (height - pad.t - pad.b) * tick / 3; const val = max - range * tick / 3; svg.append(svgNode("line", { x1: pad.l, x2: width - pad.r, y1: y, y2: y, stroke: "#dce3ed", "stroke-width": 1 }), Object.assign(svgNode("text", { x: 3, y: y + 4, fill: "#647087", "font-size": 11 }), { textContent: `${Math.round(val)} мс` })); }
      const x = (index) => pad.l + (width - pad.l - pad.r) * (samples.length <= 1 ? .5 : index / (samples.length - 1)); const y = (value) => pad.t + (height - pad.t - pad.b) * (max - value) / range;
      svg.append(svgNode("polyline", { points: points.map((point) => `${x(point.index)},${y(point.value)}`).join(" "), fill: "none", stroke: "#4263eb", "stroke-width": 2.5, "stroke-linecap": "round", "stroke-linejoin": "round" }));
      points.forEach((point) => { const circle = svgNode("circle", { cx: x(point.index), cy: y(point.value), r: 3.5, fill: "#4263eb", "data-latency-ms": point.value }); const title = svgNode("title"); title.textContent = `${localTime(point.sample.timestamp)}: ${latency(point.value)}`; circle.append(title); svg.append(circle); }); dom.chart.append(svg);
    } else dom.chart.append(element("span", "chart-empty", "В истории есть только результаты без задержки."));
    [...samples].reverse().slice(0, 12).forEach((sample) => { const item = element("li"); item.append(element("span", "", `${localTime(sample.timestamp)} · ${statusText[sample.status] || sample.status || "—"}`), element("span", "muted", `${latency(sample.latency_ms)}${sample.http_status ? ` · HTTP ${sample.http_status}` : ""}`)); dom.historyList.append(item); });
  }

  function browserNotificationsEnabled() { try { return localStorage.getItem("services-ping-browser-notifications") === "true"; } catch (_) { return false; } }
  function updateNotificationButton() { const permitted = "Notification" in window && Notification.permission === "granted"; dom.notification.textContent = browserNotificationsEnabled() && permitted ? "Уведомления браузера включены" : "Включить уведомления браузера"; }
  async function enableBrowserNotifications() {
    if (!("Notification" in window)) { toast("Этот браузер не поддерживает уведомления.", true); return; }
    const permission = await Notification.requestPermission(); if (permission === "granted") { try { localStorage.setItem("services-ping-browser-notifications", "true"); } catch (_) {} toast("Уведомления браузера включены."); } else toast("Разрешение на уведомления не получено.", true); updateNotificationButton();
  }
  function notifyOnInternetChange(current) {
    const previous = app.previousInternet; app.previousInternet = current; if (!previous || previous === current || !browserNotificationsEnabled() || !("Notification" in window) || Notification.permission !== "granted") return;
    const isChange = current === "offline" || (previous === "offline" && (current === "online" || current === "partial")); if (!isChange) return;
    try { const last = Number(localStorage.getItem("services-ping-notification-last") || 0); if (Date.now() - last < 10000) return; localStorage.setItem("services-ping-notification-last", String(Date.now())); new Notification("Services Ping", { body: current === "offline" ? "Интернет недоступен по опорным проверкам." : "Доступ к интернету восстановился." }); } catch (_) {}
  }
  function closeDialog(id) { const dialog = $(id); if (dialog && dialog.open) dialog.close(); }
  function bind() {
    $("add-target").addEventListener("click", () => openTarget()); document.addEventListener("click", (event) => { const action = event.target.closest("[data-action]"); if (action && action.dataset.action === "add-target") openTarget(); const close = event.target.closest("[data-close]"); if (close) closeDialog(close.dataset.close); });
    $("target-kind").addEventListener("change", updateKindHelp); dom.targetForm.addEventListener("submit", saveTarget); $("open-settings").addEventListener("click", openSettings); dom.settingsForm.addEventListener("submit", saveSettings); $("add-reference").addEventListener("click", () => dom.refEditor.append(referenceRow())); $("export-config").addEventListener("click", exportConfig); $("start-import").addEventListener("click", () => dom.importFile.click()); dom.importFile.addEventListener("change", selectImport); dom.importConfirm.addEventListener("change", () => { dom.importApply.disabled = !dom.importConfirm.checked; }); dom.importApply.addEventListener("click", applyImport);
    $("manual-check").addEventListener("click", manualCheck); dom.pause.addEventListener("click", togglePause); $("retry-connection").addEventListener("click", pollState); $("refresh-events").addEventListener("click", refreshEvents); dom.notification.addEventListener("click", enableBrowserNotifications);
    document.addEventListener("visibilitychange", () => { window.clearTimeout(app.pollTimer); if (!app.polling) schedulePoll(); });
  }
  async function start() { bind(); updateNotificationButton(); try { await session(); await Promise.all([pollState(), refreshEvents()]); } catch (error) { dom.backendBanner.hidden = false; toast(cleanError(error), true); schedulePoll(); } }
  start();
})();
