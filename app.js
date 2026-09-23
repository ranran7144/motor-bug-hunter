"use strict";

(() => {
  const $ = id => document.getElementById(id);
  let report = null;
  let selectedCase = null;
  let activeFilter = "all";
  let pollTimer = null;
  let busy = false;
  let renderedSignature = null;
  const questionDefinitions = [
    {key: "Q1", title: "異常か", alternatives: ["status", "anomaly"]},
    {key: "Q2", title: "原因の分類", alternatives: ["category"]},
    {key: "Q3", title: "重大度", alternatives: ["severity"]},
    {key: "Q4", title: "テスト継続", alternatives: ["continue_test", "continue"]},
    {key: "Q5", title: "詳細解析", alternatives: ["deeper_analysis", "needs_analysis"]},
  ];

  const finite = value => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 0) => finite(value) ? value.toLocaleString("ja-JP", {maximumFractionDigits: digits, minimumFractionDigits: digits}) : "—";
  const percentage = value => finite(value) ? `${(value * 100).toFixed(1)}%` : "—";
  const money = value => finite(value) ? `$${value.toLocaleString("en-US", {maximumFractionDigits: 6})}` : "—";
  const text = (id, value) => { $(id).textContent = value; };
  const create = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  };
  function isHealthy(item) {
    if (typeof item.truth === "boolean") return !item.truth;
    const value = String(item.truth ?? "").toUpperCase();
    return ["NORMAL", "HEALTHY", "CLEAN", "NEGATIVE", "FALSE", "0"].includes(value);
  }
  function bar(id, value, total = 30) {
    $(id).style.width = `${finite(value) && total > 0 ? Math.max(0, Math.min(100, value / total * 100)) : 0}%`;
  }
  function badge(value, className) { return create("span", `result-badge ${className}`, value); }
  function formatObject(value) {
    if (typeof value === "string") return value;
    if (value && typeof value === "object") {
      const detail = value.message || value.requirement || value.rule || value.id || JSON.stringify(value);
      return value.cycle != null ? `cycle ${value.cycle} · ${detail}` : detail;
    }
    return String(value);
  }

  function setBusy(value) {
    busy = value;
    $("run-button").disabled = value;
    $("provider").disabled = value;
    $("seed").disabled = value;
    $("run-button").textContent = value ? "実験を実行中…" : "▶  実験を開始";
    document.querySelector(".run-status").classList.toggle("running", value);
  }
  function showError(message) {
    $("error-banner").hidden = !message;
    $("error-banner").textContent = message || "";
  }

  async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      const body = await response.text();
      let data;
      try { data = JSON.parse(body); }
      catch { throw new Error(`サーバーの応答を読み取れませんでした (HTTP ${response.status})。Python サーバー経由で開いてください。`); }
      if (!response.ok) throw new Error(data.error || data.message || `HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timeout); }
  }

  async function refreshStatus() {
    clearTimeout(pollTimer);
    try {
      const status = await fetchJson("/api/status", {cache: "no-store"});
      setBusy(Boolean(status.running));
      text("run-phase", String(status.phase || (status.running ? "RUNNING" : "READY")).toUpperCase());
      text("run-message", status.message || "実験を開始できます。");
      showError(status.error);
      if (status.report) {
        const signature = JSON.stringify(status.report);
        if (signature !== renderedSignature) {
          renderedSignature = signature;
          renderReport(status.report);
        }
      }
      pollTimer = setTimeout(refreshStatus, status.running ? 1000 : 6000);
    } catch (error) {
      text("run-phase", "CONNECTION ERROR");
      text("run-message", "サーバーへの接続を確認しています。");
      showError(error.name === "AbortError" ? "サーバーの応答がタイムアウトしました。次の確認で再接続します。" : error.message);
      pollTimer = setTimeout(refreshStatus, 6000);
    }
  }

  $("run-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !$("run-form").reportValidity()) return;
    const seed = Number($("seed").value);
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 4294967295) {
      showError("シードは 0〜4294967295 の整数で指定してください。");
      return;
    }
    const provider = $("provider").value;
    setBusy(true);
    showError(null);
    text("run-phase", "STARTING");
    text("run-message", "実験の開始を要求しています…");
    clearTimeout(pollTimer);
    try {
      await fetchJson("/api/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({provider, seed})});
      await refreshStatus();
    } catch (error) {
      setBusy(false);
      text("run-phase", "ERROR");
      text("run-message", "実験を開始できませんでした。");
      showError(error.name === "AbortError" ? "開始要求がタイムアウトしました。サーバーの実行状態を確認します。" : error.message);
      pollTimer = setTimeout(refreshStatus, 3000);
    }
  });

  $("provider").addEventListener("change", () => {
    text("data-notice", $("provider").value === "off"
      ? "ローカルで C / SIL と従来テストを実行します。AI の判定は未実行として記録します。"
      : "要求仕様と合成トレースを選択した API に送信します。classifier.dev は API キー不要です。");
  });
  $("download-report").addEventListener("click", event => { if (!report) event.preventDefault(); });

  function renderReport(value) {
    report = value;
    const summary = report.summary || {};
    const total = summary.bug_types_total || 30;
    const metrics = {activated: summary.activated_bug_types, conventional: summary.conventional_detected, ai: summary.ai_detected, combined: summary.combined_detected};
    for (const [key, amount] of Object.entries(metrics)) {
      text(`metric-${key}`, number(amount));
      bar(`${key}-bar`, amount, total);
      if (key !== "activated") {
        bar(`compare-${key}`, amount, total);
        text(`compare-${key}-label`, number(amount));
      }
    }
    text("metric-fpr", percentage(summary.ai_false_positive_rate));
    text("metric-confidence", number(summary.mean_confidence, 3));
    text("metric-latency", finite(summary.mean_request_latency_ms) ? `${number(summary.mean_request_latency_ms)} ms` : "—");
    text("metric-cost", money(summary.cost_usd_reported));
    text("million-cost", `100 万ケース換算：${money(summary.estimated_million_cost_usd)}`);
    text("healthy-count", `判定済み正常系 ${number(summary.ai_healthy_evaluated)} / ${number(summary.healthy_windows)} 窓`);
    text("ai-metric-note", finite(summary.ai_detected) ? `${number(summary.ai_evaluated)} / ${number(summary.ai_total)} 窓を判定` : "未実行・未取得のため算出なし");
    text("comparison-note", `分母は全 ${total} 種類。${summary.activated_bug_types ?? "—"} 種類の活性化を確認。未実行・未取得は「—」で表示。`);
    text("run-id", report.run_id || "—");
    text("provenance-run", report.run_id || "—");
    text("provenance-seed", report.seed ?? "—");
    const models = Array.isArray(report.models) ? report.models.join(", ") : "";
    text("provenance-model", `${report.provider || "—"}${models ? ` / ${models}` : ""}`);
    text("provenance-windows", `${number(summary.ai_evaluated)} / ${number(summary.ai_total)}`);
    const staticAnalysis = report.static_analysis || {};
    text("static-analysis-status", staticAnalysis.status === "completed" ? `静的解析：診断 ${number(staticAnalysis.diagnostic_count ?? staticAnalysis.diagnostics?.length ?? 0)} 件` : ["error", "failed"].includes(staticAnalysis.status) ? "静的解析：解析エラー" : "静的解析：未実行");
    text("ai-model-badge", (report.models || []).length && (report.models || []).every(model => /^jev[-/]/i.test(model)) ? "JEV" : "AI");
    const notes = $("experiment-notes");
    notes.replaceChildren();
    if (report.created_at) notes.append(create("li", "", `作成日時：${report.created_at}`));
    for (const note of report.notes || []) notes.append(create("li", "", formatObject(note)));
    if (!finite(summary.cost_usd_reported)) notes.append(create("li", "", "API 報告コストがない場合、料金と 100 万ケース換算は未取得と表示します。"));
    $("download-report").classList.remove("disabled");
    $("download-report").setAttribute("aria-disabled", "false");
    const previousCategory = $("category-filter").value;
    $("category-filter").replaceChildren(create("option", "", "すべてのカテゴリ"));
    $("category-filter").firstChild.value = "all";
    const categories = [...new Set((report.cases || []).map(item => item.category).filter(Boolean))].sort();
    for (const category of categories) {
      const option = create("option", "", category);
      option.value = category;
      $("category-filter").append(option);
    }
    $("category-filter").value = categories.includes(previousCategory) ? previousCategory : "all";
    const selectedId = selectedCase?.window_id;
    selectedCase = (report.cases || []).find(item => item.window_id === selectedId) || null;
    renderCases();
    if (selectedCase) selectCase(selectedCase);
  }

  function filteredCases() {
    const query = $("case-search").value.trim().toLocaleLowerCase();
    const category = $("category-filter").value;
    return (report?.cases || []).filter(item => {
      if (category !== "all" && item.category !== category) return false;
      if (query && !`${item.window_id} ${item.bug_id || ""} ${item.title || ""} ${item.category || ""}`.toLocaleLowerCase().includes(query)) return false;
      if (activeFilter === "bugs" && isHealthy(item)) return false;
      if (activeFilter === "healthy" && !isHealthy(item)) return false;
      if (activeFilter === "ai-only" && (isHealthy(item) || item.ai_detected !== true || item.conventional_detected)) return false;
      if (activeFilter === "false-positive" && (!isHealthy(item) || item.ai_detected !== true)) return false;
      return true;
    });
  }
  function renderCases() {
    const cases = filteredCases();
    text("case-count", number(report?.cases?.length || 0));
    text("table-result-count", `${cases.length} / ${report?.cases?.length || 0} 件`);
    const body = $("case-table-body");
    body.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const item of cases) {
      const row = create("tr", item.window_id === selectedCase?.window_id ? "selected" : "");
      row.tabIndex = 0;
      row.setAttribute("aria-label", `${item.window_id} ${item.title || ""} のトレースを表示`);
      row.setAttribute("aria-selected", String(item.window_id === selectedCase?.window_id));
      const name = create("td");
      name.append(create("span", "case-title", item.title || (isHealthy(item) ? "正常系" : item.bug_id)), create("span", "case-id", item.window_id));
      const category = create("td"); category.append(create("span", "category-badge", item.category || "—"));
      const truth = create("td");
      truth.append(isHealthy(item) ? badge("正常", "normal") : item.activation_verified ? badge("活性化済み", "fault") : badge("未活性化", "unverified"));
      const conventional = create("td");
      conventional.append(item.conventional_detected ? badge("検出", "detected") : badge(isHealthy(item) ? "異常なし" : "未検出", "missed"));
      const ai = create("td");
      const aiStatus = item.ai_status;
      ai.append(badge(item.ai_detected === true ? "検出" : item.ai_detected === false ? "未検出" : "保留", item.ai_detected === true ? "detected" : "missed"));
      if (aiStatus) ai.append(create("span", "case-id", `${aiStatus} / 異常 ${percentage(item.p_anomaly)}`));
      row.append(name, category, truth, conventional, ai, create("td", "confidence-cell", percentage(item.confidence)), create("td", "row-chevron", "↗"));
      row.addEventListener("click", () => selectCase(item));
      row.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectCase(item); }
      });
      fragment.append(row);
    }
    body.append(fragment);
    $("cases-empty").hidden = cases.length > 0;
    if (!cases.length) {
      $("cases-empty").querySelector("strong").textContent = report ? "条件に合うケースがありません" : "まだ実験結果がありません";
      $("cases-empty").querySelector("p").textContent = report ? "検索条件やフィルターを変更してください。" : "実験を開始すると、ケースごとの検出結果がここに表示されます。";
    }
    if (!selectedCase && cases.length) selectCase(cases[0], false);
  }

  document.querySelectorAll(".case-tab").forEach(button => button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".case-tab").forEach(tab => {
      const selected = tab === button;
      tab.classList.toggle("selected", selected);
      tab.setAttribute("aria-pressed", String(selected));
    });
    renderCases();
  }));
  $("case-search").addEventListener("input", renderCases);
  $("category-filter").addEventListener("change", renderCases);

  function selectCase(item, rerender = true) {
    selectedCase = item;
    if (rerender) renderCases();
    else {
      const firstRow = $("case-table-body").querySelector("tr");
      if (firstRow) { firstRow.classList.add("selected"); firstRow.setAttribute("aria-selected", "true"); }
    }
    $("inspector-empty").hidden = true;
    $("inspector-content").hidden = false;
    text("selected-title", item.title || item.bug_id || "正常系");
    text("selected-id", item.window_id);
    const meta = $("selected-meta");
    meta.replaceChildren();
    meta.append(create("span", "category-badge", item.category || "OTHER"));
    if (item.severity) meta.append(create("span", "", item.severity));
    meta.append(create("span", "", `真値：${isHealthy(item) ? "正常" : item.activation_verified ? "不具合活性化を確認" : "活性化未確認"}`));
    meta.append(create("span", "", `${item.trace?.length || 0} cycles`));
    const trace = item.trace || [];
    $("cycle-index").max = Math.max(0, trace.length - 1);
    $("cycle-index").value = trace.length ? trace.length - 1 : 0;
    $("cycle-index").disabled = !trace.length;
    text("selected-model", item.model || "AI 未実行");
    renderQuestions(item);
    const violations = $("violation-list");
    violations.replaceChildren();
    if (item.violations?.length) {
      for (const violation of item.violations) violations.append(create("li", "", formatObject(violation)));
    } else violations.append(create("li", "", "この窓で決定的ルールの違反は記録されていません。"));
    updateCycleDetails();
    requestAnimationFrame(drawTrace);
  }

  function normalizeQuestion(raw) {
    if (typeof raw === "string") return {answer: raw, probabilities: {}};
    if (!raw || typeof raw !== "object") return {answer: null, probabilities: {}};
    let probabilities = raw.probabilities || raw.distribution || raw.scores || {};
    if (!Object.keys(probabilities).length) {
      probabilities = Object.fromEntries(Object.entries(raw).filter(([key, value]) => finite(value) && !["confidence", "latency_ms", "cost_usd"].includes(key)));
    }
    if (Array.isArray(probabilities)) probabilities = Object.fromEntries(probabilities.map(value => [value.label || value.class || value.answer, value.probability ?? value.score]));
    const entries = Object.entries(probabilities).filter(([, value]) => finite(value));
    const answer = raw.answer || raw.label || raw.prediction || raw.selected || (entries.length ? entries.reduce((a, b) => a[1] >= b[1] ? a : b)[0] : null);
    return {answer, probabilities: Object.fromEntries(entries)};
  }
  function renderQuestions(item) {
    const grid = $("questions-grid");
    grid.replaceChildren();
    const questions = item.questions || {};
    for (const definition of questionDefinitions) {
      let raw = questions[definition.key] ?? questions[definition.key.toLowerCase()];
      if (raw === undefined) raw = definition.alternatives.map(key => questions[key]).find(value => value !== undefined);
      const question = normalizeQuestion(raw);
      if (!question.answer && definition.key === "Q1" && item.ai_status) question.answer = item.ai_status;
      const card = create("article", "question-card");
      const heading = create("h4"); heading.append(create("span", "", definition.key), document.createTextNode(definition.title));
      card.append(heading, create("p", "question-answer", question.answer || "未実行"));
      const probabilities = Object.entries(question.probabilities).sort((a, b) => b[1] - a[1]);
      for (const [label, value] of probabilities) {
        const row = create("div", `probability-row${label === question.answer ? " winner" : ""}`);
        const labelRow = create("div", "probability-label");
        labelRow.append(create("span", "", label), create("span", "", percentage(value)));
        const track = create("div", "probability-bar");
        const fill = create("i"); fill.style.width = `${Math.max(0, Math.min(100, value * 100))}%`; track.append(fill);
        row.append(labelRow, track); card.append(row);
      }
      if (!probabilities.length) card.append(create("p", "no-probability", "確率分布：未取得"));
      grid.append(card);
    }
  }

  function updateCycleDetails() {
    const trace = selectedCase?.trace || [];
    const index = Number($("cycle-index").value);
    const sample = trace[index];
    text("cycle-label", sample ? `cycle ${sample.cycle ?? index}` : "—");
    const details = $("cycle-details");
    details.replaceChildren();
    const fields = [["state", "STATE"], ["pwm", "PWM"], ["motor_speed", "SPEED"], ["motor_current", "CURRENT"], ["temperature", "TEMP"], ["emergency_stop", "E-STOP"], ["communication_alive", "COMM ALIVE"]];
    for (const [key, label] of fields) {
      const block = create("div");
      const value = sample?.[key];
      const formatted = value == null ? "—" : typeof value === "boolean" ? value ? "TRUE" : "FALSE" : finite(value) ? number(value, Number.isInteger(value) ? 0 : 2) : String(value);
      block.append(create("dt", "", label), create("dd", "", formatted)); details.append(block);
    }
  }
  $("cycle-index").addEventListener("input", () => { updateCycleDetails(); drawTrace(); });

  function drawTrace() {
    const canvas = $("trace-canvas");
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const scale = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(bounds.width * scale);
    canvas.height = Math.round(bounds.height * scale);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(scale, 0, 0, scale, 0, 0);
    const w = bounds.width, h = bounds.height;
    const left = 43, right = w - 53, top = 20, bottom = h - 28;
    context.clearRect(0, 0, w, h);
    const trace = selectedCase?.trace || [];
    if (!trace.length) {
      context.fillStyle = "#7890a9"; context.font = "11px Segoe UI, sans-serif"; context.textAlign = "center";
      context.fillText("トレースデータなし", w / 2, h / 2); return;
    }
    const valid = key => trace.map(sample => sample[key]).filter(finite);
    const pwmValues = valid("pwm"), speedValues = valid("motor_speed");
    const pwmMin = Math.min(0, ...pwmValues), pwmMax = Math.max(100, ...pwmValues);
    const speedMin = Math.min(0, ...speedValues), speedMax = Math.max(1, ...speedValues);
    const speedRange = (speedMax - speedMin) * 1.08;
    const x = index => left + index / Math.max(1, trace.length - 1) * (right - left);
    const yPwm = value => bottom - (value - pwmMin) / (pwmMax - pwmMin) * (bottom - top);
    const ySpeed = value => bottom - (value - speedMin) / speedRange * (bottom - top);
    const step = (right - left) / Math.max(1, trace.length - 1);
    context.fillStyle = "#ff7e8f10";
    trace.forEach((sample, index) => {
      if (sample.emergency_stop) context.fillRect(Math.max(left, x(index) - step / 2), top, Math.min(step, right - Math.max(left, x(index) - step / 2)), bottom - top);
    });
    context.font = "9px Consolas, monospace";
    for (let i = 0; i <= 4; i++) {
      const y = top + i / 4 * (bottom - top);
      context.strokeStyle = "#263346"; context.lineWidth = 1;
      context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
      context.fillStyle = "#658b94"; context.textAlign = "right";
      context.fillText(number(pwmMax - i / 4 * (pwmMax - pwmMin)), left - 8, y + 3);
      context.fillStyle = "#a08a77"; context.textAlign = "left";
      context.fillText(number(speedMin + (1 - i / 4) * speedRange), right + 8, y + 3);
    }
    const ticks = Math.min(5, trace.length - 1);
    for (let i = 0; i <= ticks; i++) {
      const index = Math.round(i / Math.max(1, ticks) * (trace.length - 1));
      context.fillStyle = "#647b95"; context.textAlign = "center";
      context.fillText(String(trace[index].cycle ?? index), x(index), bottom + 18);
    }
    function line(key, y, color, stepped) {
      context.beginPath(); context.strokeStyle = color; context.lineWidth = 1.8; context.lineJoin = "round";
      let previous = null;
      trace.forEach((sample, index) => {
        if (!finite(sample[key])) { previous = null; return; }
        if (previous === null) context.moveTo(x(index), y(sample[key]));
        else {
          if (stepped) context.lineTo(x(index), y(previous));
          context.lineTo(x(index), y(sample[key]));
        }
        previous = sample[key];
      });
      context.stroke();
    }
    line("motor_speed", ySpeed, "#ffb778", false);
    line("pwm", yPwm, "#51e2d1", true);
    const selected = Math.max(0, Math.min(trace.length - 1, Number($("cycle-index").value)));
    context.setLineDash([3, 4]); context.strokeStyle = "#8096af"; context.lineWidth = 1;
    context.beginPath(); context.moveTo(x(selected), top); context.lineTo(x(selected), bottom); context.stroke(); context.setLineDash([]);
    for (const [key, y, color] of [["pwm", yPwm, "#51e2d1"], ["motor_speed", ySpeed, "#ffb778"]]) {
      if (finite(trace[selected][key])) {
        context.beginPath(); context.arc(x(selected), y(trace[selected][key]), 3, 0, Math.PI * 2); context.fillStyle = color; context.fill();
      }
    }
  }
  $("trace-canvas").addEventListener("pointerdown", event => {
    const trace = selectedCase?.trace || [];
    if (!trace.length) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const fraction = (event.clientX - bounds.left - 43) / (bounds.width - 96);
    $("cycle-index").value = Math.max(0, Math.min(trace.length - 1, Math.round(fraction * (trace.length - 1))));
    updateCycleDetails(); drawTrace();
  });
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => requestAnimationFrame(drawTrace)).observe($("trace-canvas").parentElement);
  else window.addEventListener("resize", drawTrace);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  refreshStatus();
})();
