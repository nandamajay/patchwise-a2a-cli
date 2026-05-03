(() => {
  const state = {
    sessionId: null,
    terminals: {},
    fitAddons: {},
    ws: {},
    eventWs: null,
    report: null,
    rounds: [],
  };

  const paneThemes = {
    chanakya: { background: "#0d2626", foreground: "#00ffd0" },
    aryabhata: { background: "#1a0d2e", foreground: "#c084fc" },
    orchestrator: { background: "#0d0d0d", foreground: "#94a3b8" },
  };

  const el = (id) => document.getElementById(id);

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${text}`);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function setLiveUi(sessionId) {
    state.sessionId = sessionId;
    el("session-id-pill").textContent = sessionId;
    el("live-badge").classList.remove("hidden");
    el("btn-stop").classList.remove("hidden");
    el("btn-report").classList.remove("hidden");
    el("screen-attach-cmd").textContent = `screen -r patchwise-${sessionId}`;
  }

  function initTerminal(elementId, theme) {
    if (state.terminals[elementId]) return state.terminals[elementId];
    const terminal = new window.Terminal({
      convertEol: true,
      cursorBlink: true,
      fontSize: 12,
      theme,
      scrollback: 5000,
    });
    const fitAddon = new window.FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(el(elementId));
    fitAddon.fit();
    state.terminals[elementId] = terminal;
    state.fitAddons[elementId] = fitAddon;
    return terminal;
  }

  function connectTerminalWS(terminal, wsUrl) {
    const ws = new WebSocket(wsUrl);
    ws.onmessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data);
        terminal.write(`${payload.line || ""}\r\n`);
      } catch {
        terminal.write(`${evt.data}\r\n`);
      }
    };
    ws.onerror = () => terminal.write("[websocket error]\r\n");
    ws.onclose = () => terminal.write("[websocket closed]\r\n");
    return ws;
  }

  async function startSession(task, watchPath, rounds, openScreen, openTerminals) {
    const payload = {
      task,
      watch_path: watchPath,
      max_rounds: Number(rounds),
      open_screen: openScreen,
      open_web_terminals: openTerminals,
    };
    const status = await api("/api/session/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setLiveUi(status.session_id);
    if (openTerminals) {
      connectAllTerminalStreams(status.session_id);
    }
    connectEventStream(status.session_id);
    await loadReport(status.session_id);
    return status;
  }

  async function stopSession() {
    if (!state.sessionId) return;
    await api(`/api/session/${state.sessionId}/stop`, { method: "POST" });
    el("live-badge").classList.add("hidden");
  }

  async function loadSessions() {
    const sessions = await api("/api/sessions");
    if (Array.isArray(sessions) && sessions.length > 0) {
      const active = sessions.find((s) => s.status === "running") || sessions[0];
      if (active?.session_id) {
        setLiveUi(active.session_id);
        await loadReport(active.session_id);
      }
    }
    return sessions;
  }

  function connectAllTerminalStreams(sessionId) {
    closeTerminalSockets();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const base = `${proto}://${location.host}/ws/${sessionId}`;
    state.ws.chanakya = connectTerminalWS(state.terminals.chanakya, `${base}/builder`);
    state.ws.aryabhata = connectTerminalWS(state.terminals.aryabhata, `${base}/reviewer`);
    state.ws.orchestrator = connectTerminalWS(state.terminals.orchestrator, `${base}/orchestrator`);
  }

  function closeTerminalSockets() {
    Object.values(state.ws).forEach((ws) => {
      try { ws.close(); } catch (_) {}
    });
    state.ws = {};
  }

  function connectEventStream(sessionId) {
    if (state.eventWs) {
      try { state.eventWs.close(); } catch (_) {}
    }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${sessionId}/events`);
    ws.onmessage = (evt) => {
      let event;
      try {
        event = JSON.parse(evt.data);
      } catch {
        return;
      }
      if (!event || !event.type) return;

      if (event.type === "round_start") onRoundStart(event.round);
      if (event.type === "gate_result") onGateResult(getActiveRound(), event.result);
      if (event.type === "finding") onFinding(event);
      if (event.type === "scores") onScores(event);
      if (event.type === "lgtm") onLGTM();
      if (event.type === "round_summary") onRoundSummary(event.summary);
    };
    state.eventWs = ws;
  }

  function getActiveRound() {
    const active = document.querySelector("#round-pills .round-pill.active");
    if (!active) return 1;
    return Number(active.dataset.round || 1);
  }

  function onRoundStart(round) {
    const pills = document.querySelectorAll("#round-pills .round-pill");
    pills.forEach((pill) => pill.classList.remove("active"));
    const target = document.querySelector(`#round-pills .round-pill[data-round='${round}']`);
    if (target) {
      target.classList.remove("pending");
      target.classList.add("active");
      target.textContent = `R${round}`;
    }
  }

  function onGateResult(round, result) {
    const target = document.querySelector(`#round-pills .round-pill[data-round='${round}']`);
    if (!target) return;
    target.classList.remove("active", "pending", "pass", "fail");
    target.classList.add(result === "pass" ? "pass" : "fail");
    target.textContent = `R${round} ${result === "pass" ? "✓" : "!"}`;
  }

  function onFinding(finding) {
    addNegotiationEntry(getActiveRound(), "aryabhata", "finding", {
      severity: finding.severity || "low",
      location: finding.location || "",
      content: finding.desc || "",
      id: finding.id || "",
    });
  }

  function onScores(scores) {
    updateScore("builder", Number(scores.builder_confidence || 0));
    updateScore("reviewer", Number(scores.reviewer_confidence || 0));
    updateScore("gauge", Number(scores.builder_patch_gauge || 0));
  }

  function onLGTM() {
    el("lgtm-banner").classList.remove("hidden");
    updateChecklist({
      functional: "passed",
      error_paths: "passed",
      checkpatch: "passed",
      commit_msg: "passed",
      v1_comments: "passed",
      fix_strategy: "passed",
    });
  }

  function onRoundSummary(summary) {
    if (!summary) return;
    const findings = summary.findings || {};
    el("stat-total").textContent = findings.total ?? "—";
    el("stat-open").textContent = findings.open ?? "—";
    el("stat-closed").textContent = findings.closed ?? "—";

    const prior = (summary.prior_comments && summary.prior_comments.totals) || summary.prior_comments || {};
    el("stat-prior").textContent = prior.received_total ?? prior.received ?? "—";

    const builder = summary.builder || {};
    const reviewer = summary.reviewer || {};
    updateScore("builder", Number(builder.confidence || 0));
    updateScore("reviewer", Number(reviewer.confidence || 0));
    updateScore("gauge", Number(builder.patch_gauge || 0));
  }

  async function loadReport(sessionId) {
    const report = await api(`/api/session/${sessionId}/report`);
    state.report = report;
    state.rounds = Array.isArray(report.rounds) ? report.rounds : [];

    renderRoundPills(report.max_rounds || 3, report.rounds || []);
    renderRounds(report.rounds || []);
    renderPriorComments(report.prior_comments || []);
    renderPatchSeries(report);
    renderPatchFilesFromRounds(report.rounds || []);
    renderNegotiationFromRounds(report.rounds || []);
    updateChecklist(report.lgtm_checklist || {});

    const latest = (report.rounds || []).slice(-1)[0];
    if (latest) {
      el("stat-total").textContent = latest.findings?.total ?? "—";
      el("stat-open").textContent = latest.findings?.open ?? "—";
      el("stat-closed").textContent = latest.findings?.closed ?? "—";
      el("stat-prior").textContent = latest.prior_comments?.received ?? "—";
      updateScore("builder", Number(latest.scores?.builder_confidence || 0));
      updateScore("reviewer", Number(latest.scores?.reviewer_confidence || 0));
      updateScore("gauge", Number(latest.scores?.builder_patch_gauge || 0));

    }

    if ((report.final_status || "").toLowerCase() === "lgtm") {
      onLGTM();
      el("output-path-display").textContent = report.watch_path || "LGTM";
      el("lgtm-output-path").textContent = report.watch_path || "";
    }

    return report;
  }

  function renderPriorComments(comments) {
    const container = el("v1-thread");
    container.innerHTML = "";
    comments.forEach((c) => {
      const row = document.createElement("div");
      row.className = "round-item";
      row.innerHTML = `
        <div><span class="sev-chip low">${c.from || "reviewer"}</span>${c.subject || ""}</div>
        <div>${c.addressed ? "✅ addressed" : "⏳ open"}</div>
      `;
      container.appendChild(row);
    });
  }

  function renderRounds(rounds) {
    const tracker = el("rounds-tracker");
    tracker.innerHTML = "";

    rounds.forEach((r) => {
      const item = document.createElement("div");
      item.className = "round-item";
      const gateCls = r.gate === "pass" ? "gate-pass" : "gate-fail";
      const top = (r.top_open && r.top_open[0]) || {};
      item.innerHTML = `
        <div><strong>Round ${r.round}</strong> · <span class="${gateCls}">${r.gate.toUpperCase()}</span></div>
        <div>B:${r.scores.builder_confidence || 0} R:${r.scores.reviewer_confidence || 0} G:${r.scores.builder_patch_gauge || 0}</div>
        <div>${top.title || top.description || "No open findings"}</div>
      `;
      tracker.appendChild(item);
    });
  }

  function renderPatchFiles(files) {
    const container = el("patch-files");
    container.innerHTML = "";
    files.forEach((f) => {
      const row = document.createElement("div");
      row.className = "round-item";
      const sev = (f.severity || "ok").toLowerCase();
      row.innerHTML = `<span>${f.name || "unknown"}</span> <span class="sev-chip ${sev}">${sev}</span>`;
      container.appendChild(row);
    });
  }

  function renderPatchFilesFromRounds(rounds) {
    const map = new Map();
    rounds.forEach((r) => {
      const items = r.findings?.items || [];
      items.forEach((item) => {
        const loc = item.location || "";
        const name = loc.includes(":") ? loc.split(":")[0] : loc;
        if (!name) return;
        const sev = String(item.severity || "low").toLowerCase();
        map.set(name, { name, severity: sev });
      });
    });
    renderPatchFiles(Array.from(map.values()));
  }

  function renderPatchSeries(report) {
    el("patch-series-info").textContent = report.watch_path || "—";
    el("lore-context").textContent = `Task: ${report.task || "—"}`;
    el("output-path-display").textContent = report.watch_path || "Pending LGTM...";
    el("lgtm-output-path").textContent = report.watch_path || "";
  }

  function addNegotiationEntry(round, agent, type, content) {
    const container = el("negotiation-thread");
    const bubble = document.createElement("div");
    bubble.className = `bubble ${agent}`;

    let body = String(content.content || "");
    if (type === "finding") {
      body = `<span class="sev-chip ${(content.severity || "low").toLowerCase()}">${content.severity || "LOW"}</span> ${body} <small>${content.location || ""}</small>`;
    }
    if (type === "verdict") {
      body = `<strong>${content.content || ""}</strong>`;
    }
    if (type === "objection") {
      body = `${body} <a href="#">evidence</a>`;
    }

    bubble.innerHTML = `
      <div class="bubble-header">R${round} · ${agent.toUpperCase()} · ${type}</div>
      <div>${body}</div>
    `;
    container.appendChild(bubble);
    container.scrollTop = container.scrollHeight;
  }

  function renderNegotiationFromRounds(rounds) {
    const container = el("negotiation-thread");
    container.innerHTML = "";
    rounds.forEach((round) => {
      const items = round.findings?.items || [];
      items.forEach((item) => {
        addNegotiationEntry(round.round, "aryabhata", "finding", {
          severity: item.severity || "low",
          location: item.location || "",
          content: item.description || "",
          id: item.id || "",
        });
      });
      if ((round.findings?.open || 0) === 0) {
        addNegotiationEntry(round.round, "chanakya", "verdict", {
          content: "All findings addressed for this round",
        });
      }
    });
  }

  function updateChecklist(checklist) {
    const map = {
      functional: "chk-functional",
      error_paths: "chk-error-paths",
      checkpatch: "chk-checkpatch",
      commit_msg: "chk-commit-msg",
      v1_comments: "chk-v1-comments",
      fix_strategy: "chk-fix-strategy",
    };

    Object.entries(map).forEach(([key, id]) => {
      const row = el(id);
      if (!row) return;
      const icon = row.querySelector(".check-icon");
      const status = String(checklist[key] || "pending").toLowerCase();

      row.classList.remove("pass", "fail", "partial", "pending");
      if (status === "passed" || status === "pass") {
        row.classList.add("pass");
        icon.textContent = "✅";
      } else if (status === "failed" || status === "fail") {
        row.classList.add("fail");
        icon.textContent = "❌";
      } else if (status === "partial" || status === "warn") {
        row.classList.add("partial");
        icon.textContent = "⚠️";
      } else {
        row.classList.add("pending");
        icon.textContent = "⏳";
      }
    });
  }

  function updateScore(kind, value) {
    const clamped = Math.max(0, Math.min(100, Number(value || 0)));
    const bar = el(`score-${kind}`);
    const val = el(`score-${kind}-val`);
    if (!bar || !val) return;
    bar.style.width = `${clamped}%`;
    val.textContent = String(clamped);
  }

  function renderRoundPills(maxRounds, rounds) {
    const root = el("round-pills");
    root.innerHTML = "";
    const byRound = new Map((rounds || []).map((r) => [Number(r.round), r]));

    for (let i = 1; i <= Number(maxRounds || 3); i += 1) {
      const pill = document.createElement("span");
      pill.className = "round-pill pending";
      pill.dataset.round = String(i);
      pill.textContent = `R${i}`;
      const r = byRound.get(i);
      if (r) {
        pill.classList.remove("pending");
        pill.classList.add(r.gate === "pass" ? "pass" : "fail");
        pill.textContent = `R${i} ${r.gate === "pass" ? "✓" : "!"}`;
      }
      root.appendChild(pill);
    }
  }

  function bindUi() {
    el("btn-new-session").addEventListener("click", () => {
      el("modal-new-session").classList.remove("hidden");
    });

    el("btn-cancel").addEventListener("click", () => {
      el("modal-new-session").classList.add("hidden");
    });

    el("btn-start-confirm").addEventListener("click", async () => {
      try {
        const task = el("inp-task").value.trim();
        const path = el("inp-path").value.trim();
        const rounds = Number(el("inp-rounds").value || 3);
        const openScreen = el("chk-screen").checked;
        const openTerminals = el("chk-terminals").checked;
        const status = await startSession(task, path, rounds, openScreen, openTerminals);
        if (status?.session_id) setLiveUi(status.session_id);
        el("modal-new-session").classList.add("hidden");
      } catch (err) {
        window.alert(`Start failed: ${err.message}`);
      }
    });

    el("btn-stop").addEventListener("click", async () => {
      try {
        await stopSession();
      } catch (err) {
        window.alert(`Stop failed: ${err.message}`);
      }
    });

    el("btn-report").addEventListener("click", async () => {
      if (!state.sessionId) return;
      await loadReport(state.sessionId);
    });

    el("btn-copy-screen").addEventListener("click", async () => {
      await navigator.clipboard.writeText(el("screen-attach-cmd").textContent || "");
    });

    el("btn-copy-path").addEventListener("click", async () => {
      await navigator.clipboard.writeText(el("lgtm-output-path").textContent || "");
    });

    el("btn-view-diff").addEventListener("click", () => {
      if (!state.sessionId) return;
      window.open(`/api/session/${state.sessionId}/report`, "_blank");
    });

    window.addEventListener("resize", () => {
      Object.values(state.fitAddons).forEach((fit) => {
        try { fit.fit(); } catch (_) {}
      });
    });
  }

  async function boot() {
    state.terminals.chanakya = initTerminal("chanakya-terminal", paneThemes.chanakya);
    state.terminals.aryabhata = initTerminal("aryabhata-terminal", paneThemes.aryabhata);
    state.terminals.orchestrator = initTerminal("orchestrator-terminal", paneThemes.orchestrator);

    bindUi();
    const sessions = await loadSessions();

    const target = sessions.find((s) => s.session_id === "sess-20260503-090049-420846");
    if (target) {
      setLiveUi(target.session_id);
      connectAllTerminalStreams(target.session_id);
      connectEventStream(target.session_id);
      await loadReport(target.session_id);
    } else if (state.sessionId) {
      connectAllTerminalStreams(state.sessionId);
      connectEventStream(state.sessionId);
    }
  }

  boot().catch((err) => {
    console.error(err);
    addNegotiationEntry(0, "aryabhata", "objection", { content: `Boot error: ${err.message}` });
  });
})();
