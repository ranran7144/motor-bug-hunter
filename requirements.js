"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const defaults = [
    {id: "ambiguity", title: "曖昧性", labels: ["ambiguous", "clear"]},
    {id: "testability", title: "検証可能性", labels: ["testable", "not_testable"]},
    {id: "atomicity", title: "単一要件性", labels: ["atomic", "compound"]},
    {id: "subject", title: "主体", labels: ["specified", "missing"]},
    {id: "trigger", title: "トリガ条件", labels: ["specified", "missing"]},
    {id: "completion", title: "完了条件", labels: ["specified", "missing"]},
    {id: "quantitative", title: "定量条件", labels: ["sufficient", "insufficient"]},
    {id: "safety", title: "安全関連", labels: ["safety_related", "ordinary"]},
  ];
  const negative = new Set(["ambiguous", "not_testable", "compound", "missing", "insufficient"]);
  let questions = defaults;
  let catalogue = [];
  let report = null;
  let selectedId = null;
  let ready = false;
  let busy = false;
  let pollTimer;
  let lastReportSignature = "";
  const selected = new Set();
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const pct = value => finite(value) ? `${(value * 100).toFixed(1)}%` : "—";
  const count = value => finite(value) ? value.toLocaleString("ja-JP") : "—";
  const put = (id, value) => { $(id).textContent = value; };
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  };
  const questionAnswer = (result, question) => result.questions?.[question.id];
  const valid = (answer, question) => answer && !answer.abstained && question.labels.includes(answer.label);
  const pending = result => questions.some(question => !valid(questionAnswer(result, question), question));
  const safetyRelated = result => {
    const question = questions.find(item => item.id === "safety");
    const answer = question && questionAnswer(result, question);
    return question && valid(answer, question) && answer.label === "safety_related";
  };
  const needsReview = result => questions.some(question => question.id !== "safety" && valid(questionAnswer(result, question), question) && negative.has(questionAnswer(result, question).label));
  const labelsClass = (label, question) => question.id === "safety" ? (label === "safety_related" ? "safety" : "") : (negative.has(label) ? "issue" : "good");
  const badge = (answer, question) => element("span", `req-label ${valid(answer, question) ? labelsClass(answer.label, question) : "pending"}`, valid(answer, question) ? answer.label : "保留");
  function error(message) {
    $("req-error").hidden = !message;
    put("req-error", message || "");
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      let data;
      try { data = await response.json(); }
      catch { throw new Error(`応答を読み取れませんでした (HTTP ${response.status})。Python サーバー経由で開いてください。`); }
      if (!response.ok) throw new Error(data.error || data.message || `HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timer); }
  }
  function syncInput() {
    const documentMode = $("input-mode").value === "document";
    $("req-custom-input").hidden = documentMode;
    $("req-document-input").hidden = !documentMode;
    $("requirement-text").required = !documentMode;
    $("requirement-text").disabled = busy || documentMode;
    put("req-character-count", `${Array.from($("requirement-text").value).length} 文字`);
    put("req-selection-count", `${$("req-all").checked ? catalogue.length : selected.size} 件を選択`);
    const amount = documentMode ? ($("req-all").checked ? catalogue.length : selected.size) : 1;
    $("req-run").disabled = busy || !ready || (documentMode && !amount) || (!documentMode && !$("requirement-text").value.trim());
    put("req-run", busy ? "判定を実行中…" : `${documentMode ? `${amount} 件を ` : ""}8 つの観点で判定する`);
    put("req-context-note", $("context-mode").value === "document"
      ? "選択した要件文・8 つの判定基準・要件仕様書の共通定義を API に送信します。自由入力にも同じ共通定義が適用されます。"
      : "選択した要件文と 8 つの判定基準を API に送信します。仕様書の共通定義を補わず、文章自体を評価します。");
    for (const id of ["input-mode", "context-mode", "req-provider", "req-all"]) $(id).disabled = busy;
    document.querySelectorAll(".req-catalogue-row input").forEach(input => { input.disabled = busy || $("req-all").checked; input.checked = $("req-all").checked || selected.has(input.value); });
    $("req-status").classList.toggle("running", busy);
  }
  function renderCatalogue() {
    const query = $("req-catalogue-search").value.trim().toLowerCase();
    const container = $("req-catalogue");
    container.replaceChildren();
    for (const requirement of catalogue) {
      if (query && !`${requirement.requirement_id} ${requirement.text} ${requirement.section || ""}`.toLowerCase().includes(query)) continue;
      const row = element("label", "req-catalogue-row");
      const input = element("input");
      input.type = "checkbox";
      input.value = requirement.requirement_id;
      input.checked = selected.has(requirement.requirement_id);
      input.addEventListener("change", () => { input.checked ? selected.add(input.value) : selected.delete(input.value); syncInput(); });
      const content = element("span");
      content.append(element("b", "", requirement.requirement_id), element("span", "", requirement.text));
      if (requirement.section) content.append(element("small", "", requirement.section));
      row.append(input, content);
      container.append(row);
    }
    if (!container.childElementCount) container.append(element("p", "empty-state req-muted", catalogue.length ? "検索に一致する要件はありません。" : "要件を読み込めませんでした。"));
    syncInput();
  }
  function renderHead() {
    $("req-results-head").replaceChildren(element("th", "", "要件"), ...questions.map(question => element("th", "", question.title)));
    $("req-results-head").querySelectorAll("th").forEach(header => { header.scope = "col"; });
  }
  function filteredResults() {
    const filter = $("req-result-filter").value;
    return (report?.results || []).filter(result => filter === "review" ? needsReview(result) : filter === "safety" ? safetyRelated(result) : filter === "pending" ? pending(result) : true);
  }
  function renderRows() {
    const rows = filteredResults();
    const body = $("req-results-body");
    body.replaceChildren();
    for (const result of rows) {
      const row = element("tr", result.requirement_id === selectedId ? "selected" : "");
      const first = element("td");
      const button = element("button", "req-row-button");
      button.type = "button";
      button.setAttribute("aria-label", `${result.requirement_id} の判定詳細`);
      button.setAttribute("aria-pressed", String(result.requirement_id === selectedId));
      button.append(element("small", "", result.requirement_id), element("span", "", result.text));
      first.append(button);
      row.append(first);
      for (const question of questions) {
        const answer = questionAnswer(result, question);
        const cell = element("td");
        cell.append(badge(answer, question));
        cell.append(element("span", "req-probability", valid(answer, question) ? pct(answer.confidence) : "回答なし"));
        row.append(cell);
      }
      row.addEventListener("click", () => { selectedId = result.requirement_id; renderRows(); renderDetail(); });
      body.append(row);
    }
    $("req-results-empty").hidden = rows.length > 0;
    if (report) {
      $("req-results-empty").querySelector("strong").textContent = report.results?.length ? "条件に一致する要件はありません" : "有効な判定結果はまだありません";
      $("req-results-empty").querySelector("p").textContent = report.results?.length ? "表示の絞り込みを変更してください。" : "実行状況とエラーを確認してください。";
    }
    put("req-visible-count", `${rows.length} / ${report?.results?.length || 0} 件を表示`);
  }
  function renderDetail() {
    const result = report?.results?.find(item => item.requirement_id === selectedId);
    $("req-detail-empty").hidden = Boolean(result);
    $("req-detail-content").hidden = !result;
    if (!result) return;
    put("req-detail-title", "8 つの観点の回答");
    put("req-detail-id", result.requirement_id);
    put("req-detail-text", result.text);
    const summary = [];
    if (needsReview(result)) summary.push("品質に関する指摘があります。");
    else if (!pending(result)) summary.push("品質 7 観点では指摘なしと判定されました。");
    if (pending(result)) summary.push("未回答の観点は保留です。");
    if (safetyRelated(result)) summary.push("安全関連の要件として分類されました。");
    put("req-detail-summary", summary.join(" "));
    const context = result.context;
    put("req-detail-context", context ? (typeof context === "string" ? context : JSON.stringify(context, null, 2)) : "要件文のみ。仕様書の共通定義は付与していません。");
    const cards = $("req-question-grid");
    cards.replaceChildren();
    questions.forEach((question, index) => {
      const answer = questionAnswer(result, question);
      const card = element("article", "req-question-card");
      const heading = element("h3", "req-question-title");
      heading.append(element("span", "req-question-number", String(index + 1).padStart(2, "0")), element("span", "", question.title));
      card.append(heading, badge(answer, question));
      for (const label of question.labels) {
        const value = answer?.scores?.[label];
        const score = element("div", `req-score ${valid(answer, question) && answer.label === label ? "selected" : ""}`);
        const line = element("div", "req-score-heading");
        line.append(element("span", "", label), element("span", "", pct(value)));
        const track = element("div", "req-score-track");
        const fill = element("span");
        fill.style.width = `${finite(value) ? Math.min(100, Math.max(0, value * 100)) : 0}%`;
        track.append(fill);
        score.append(line, track);
        card.append(score);
      }
      const meta = element("div", "req-question-meta");
      meta.append(element("div", "", `confidence: ${valid(answer, question) ? pct(answer.confidence) : "保留"}`), element("div", "", `${answer?.provider || report.provider || "不明"} / ${answer?.model || "モデル未確認"}`));
      card.append(meta);
      cards.append(card);
    });
  }
  function renderReport(next) {
    report = next;
    const results = Array.isArray(report.results) ? report.results : [];
    const summary = report.summary || {};
    if (!results.some(item => item.requirement_id === selectedId)) selectedId = results[0]?.requirement_id || null;
    put("req-total", count(summary.requirements ?? results.length));
    put("req-completed", `${count(summary.completed ?? results.filter(result => !pending(result)).length)} 要件で 8 観点の回答完了`);
    put("req-review-count", count(results.filter(needsReview).length));
    put("req-abstained-count", count(results.filter(pending).length));
    put("req-safety-count", count(results.filter(safetyRelated).length));
    put("req-result-count", results.length);
    const contextName = report.context_mode === "document" ? "仕様書の共通定義を含む" : "要件文のみ";
    put("req-results-note", `この結果の前提：${contextName}。行を選ぶと、全ラベルの確率と使用モデルを確認できます。`);
    const models = Array.isArray(report.models) ? report.models : [];
    put("req-record-model", `${report.provider || "未確認"} / ${models.length ? models.join(", ") : "モデル未確認"}`);
    const requests = report.requests;
    put("req-record-requests", Array.isArray(requests) ? `${requests.length} リクエスト` : (finite(requests) ? `${requests} リクエスト` : "—"));
    put("req-record-context", contextName);
    const date = report.created_at ? new Date(report.created_at) : null;
    put("req-record-created", date && !Number.isNaN(date.getTime()) ? date.toLocaleString("ja-JP") : (report.created_at || "—"));
    put("req-run-id", report.run_id || "—");
    put("req-record-note", "1 要件につき 8 観点を判定します。API リクエスト数は送信の分割や再試行によって要件数と異なります。confidence は正解率を表しません。");
    const errors = Array.isArray(report.errors) ? report.errors : [];
    $("req-run-errors").hidden = !errors.length;
    put("req-run-errors", errors.map(item => typeof item === "string" ? item : JSON.stringify(item)).join("\n"));
    $("req-download").classList.remove("disabled");
    $("req-download").removeAttribute("aria-disabled");
    renderRows();
    renderDetail();
  }
  async function refreshStatus() {
    clearTimeout(pollTimer);
    try {
      const status = await request("/api/requirements/status", {cache: "no-store"});
      busy = Boolean(status.running);
      put("req-phase", String(status.phase || (busy ? "RUNNING" : "READY")).toUpperCase());
      put("req-message", status.message || (busy ? "判定を実行しています。" : "判定を開始できます。"));
      if (status.error) error(status.error);
      if (status.report) {
        const signature = JSON.stringify(status.report);
        if (signature !== lastReportSignature) { lastReportSignature = signature; renderReport(status.report); }
      }
      syncInput();
    } catch (exception) {
      error(`実行状況を取得できません。${exception.message}`);
      put("req-phase", "CONNECTION ERROR");
      put("req-message", "接続を再試行します。");
    } finally { pollTimer = setTimeout(refreshStatus, busy ? 1200 : 6000); }
  }
  $("req-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !ready) return;
    const documentMode = $("input-mode").value === "document";
    const payload = {provider: $("req-provider").value, context_mode: $("context-mode").value, mode: documentMode ? ($("req-all").checked ? "all" : "selected") : "custom"};
    if (documentMode) payload.requirement_ids = Array.from(selected);
    else payload.text = $("requirement-text").value.trim();
    if ((payload.mode === "selected" && !payload.requirement_ids.length) || (payload.mode === "custom" && !payload.text)) return;
    busy = true;
    syncInput();
    error("");
    put("req-phase", "STARTING");
    put("req-message", "判定を開始しています…");
    try {
      await request("/api/requirements/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      await refreshStatus();
    } catch (exception) {
      busy = false;
      syncInput();
      error(exception.message);
      await refreshStatus();
    }
  });
  for (const id of ["input-mode", "context-mode", "req-all"]) $(id).addEventListener("change", syncInput);
  $("requirement-text").addEventListener("input", syncInput);
  $("req-catalogue-search").addEventListener("input", renderCatalogue);
  $("req-result-filter").addEventListener("change", renderRows);
  async function initialize() {
    renderHead();
    syncInput();
    const outcomes = await Promise.allSettled([
      request("/api/requirements/catalogue", {cache: "no-store"}),
      refreshStatus(),
    ]);
    const catalogueOutcome = outcomes[0];
    if (catalogueOutcome.status === "fulfilled") {
      const data = catalogueOutcome.value;
      catalogue = Array.isArray(data.requirements) ? data.requirements : [];
      if (Array.isArray(data.questions) && data.questions.length === 8) questions = data.questions;
      put("req-document-title", data.document?.title || "小型モータ制御 ECU 要件仕様書");
      put("req-document-version", `${data.document?.version || ""} · ${catalogue.length} 要件`);
      ready = true;
      renderHead();
      renderCatalogue();
      if (report) renderReport(report);
    } else {
      error(`要件一覧を取得できません。${catalogueOutcome.reason.message} ページを再読み込みしてください。`);
      put("req-document-title", "要件一覧の読み込みに失敗しました");
    }
    for (const outcome of outcomes) if (outcome.status === "rejected" && outcome !== catalogueOutcome) error(outcome.reason.message);
    syncInput();
  }
  initialize();
})();
