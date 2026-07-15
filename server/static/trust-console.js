/**
 * Pre-qual trust console — customer/engineer view, ribbon, receipt, ledger, gates,
 * scorecard, export. Driven by AgentEvent { event: "trust" } + existing metrics.
 * Does not own call control.
 */
(function () {
  const GATE_TTFA = 0.8;
  const GATE_INTERRUPT = 300;
  const GATE_TURNS = 1;

  /** Live scorecard pills from /api/scorecard (text eval harness). */
  let liveScorecard = null;

  const IDLE_RIBBON = [
    { id: "classify", label: "CLASSIFY · human", status: "active" },
    { id: "intro", label: "INTRO", status: "pending" },
    { id: "qualify", label: "QUALIFY", status: "pending" },
  ];

  const state = {
    engineer: false,
    callShort: "—",
    callId: null,
    stage: "CLASSIFY",
    ribbon: IDLE_RIBBON.map((n) => ({ ...n })),
    intakeFrozen: false,
    freezeAt: null,
    disposition: null,
    receiptLines: [],
    receiptHeld: null,
    promises: [],
    interruptMs: null,
    turnsAfterOptOut: 0,
    ttfaSamples: [],
    gateTargets: {
      ttfaP50s: GATE_TTFA,
      interruptMs: GATE_INTERRUPT,
      turnsAfterOptOut: GATE_TURNS,
    },
    engLog: [],
    pendingGateBanner: null,
    lastPromiseId: null,
    silence: { status: "idle" },
    receiptEvent: null,
    receiptClock: null,
    receiptDomCount: 0,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function fmtS(ms) {
    if (ms == null || Number.isNaN(Number(ms))) return "—";
    return `${(Number(ms) / 1000).toFixed(2)} s`;
  }

  function median(arr) {
    if (!arr.length) return null;
    const a = [...arr].sort((x, y) => x - y);
    const m = Math.floor(a.length / 2);
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }

  function setView(eng) {
    state.engineer = !!eng;
    document.body.classList.toggle("view-eng", state.engineer);
    document.body.classList.toggle("view-cust", !state.engineer);
    const bE = $("btnEngView");
    const bC = $("btnCustView");
    if (bE) bE.setAttribute("aria-pressed", String(state.engineer));
    if (bC) bC.setAttribute("aria-pressed", String(!state.engineer));
  }

  function renderRibbon() {
    const host = $("trustRibbonStages");
    if (!host) return;
    const nodes = state.ribbon.length ? state.ribbon : IDLE_RIBBON;
    host.innerHTML = "";
    nodes.forEach((n, i) => {
      const wrap = document.createElement("span");
      wrap.className = "stage";
      const node = document.createElement("span");
      node.className = `node ${n.status || "pending"}`;
      node.textContent = n.label;
      wrap.appendChild(node);
      if (i < nodes.length - 1) {
        const link = document.createElement("span");
        const prevDone = n.status === "done" || n.status === "active";
        link.className = `link${prevDone && nodes[i + 1].status !== "pending" ? " done" : n.status === "done" ? " done" : ""}`;
        wrap.appendChild(link);
      }
      host.appendChild(wrap);
    });
    renderSilenceChip();
  }

  function renderSilenceChip() {
    const el = $("trustSilenceChip");
    if (!el) return;
    const s = state.silence || {};
    if (s.status === "armed" && s.nextGapS != null) {
      el.innerHTML = `Silence <b class="ok">watching · ${Number(s.nextGapS).toFixed(0)}s</b>`;
    } else if (s.status === "checkin") {
      el.innerHTML = `Silence <b>check-in ${s.checkinIndex || s.checkinsDone || "…"}</b>`;
    } else if (s.status === "close") {
      el.innerHTML = `Silence <b>no-response close</b>`;
    } else if (state.stage === "QUALIFY") {
      el.innerHTML = `Silence <b>ready after reply</b>`;
    } else {
      el.innerHTML = `Silence <b>—</b>`;
    }
  }

  /** Idle / about-to-call — highlight CLASSIFY until the live session begins. */
  function setPreCallRibbon() {
    state.stage = "CLASSIFY";
    state.ribbon = IDLE_RIBBON.map((n) => ({ ...n }));
    renderRibbon();
  }

  function updateReceiptHead() {
    const head = $("trustReceiptHead");
    if (!head) return;
    if (!state.receiptLines.length) {
      head.innerHTML = `<b>AUDIT COPY</b><span>Waiting for call…</span>`;
      return;
    }
    const clock = state.receiptClock || "";
    const event =
      state.receiptEvent ||
      (state.disposition ? String(state.disposition).replace(/_/g, " ") + " event" : "in progress");
    const meta = [state.callShort && state.callShort !== "—" ? `call ${state.callShort}` : null, clock, event]
      .filter(Boolean)
      .join(" · ");
    head.innerHTML = `<b>AUDIT COPY</b><span>${escapeHtml(meta)}</span>`;
  }

  function formatReceiptMessage(line) {
    let html = escapeHtml(line.text || "");
    const marks = [];
    if (line.recordId) marks.push(String(line.recordId));
    if (line.highlight) marks.push(String(line.highlight));
    marks
      .sort((a, b) => b.length - a.length)
      .forEach((m) => {
        const esc = escapeHtml(m);
        if (!esc || html.indexOf(esc) === -1) return;
        html = html.split(esc).join(`<span class="idm">${esc}</span>`);
      });
    // Caller quotes survive escape as &quot;…&quot;
    html = html.replace(/&quot;([^&]+)&quot;/g, '<span class="idm">"$1"</span>');
    if (line.ok) html += ` <span class="ok">✓</span>`;
    return html;
  }

  function appendReceiptDomLine(line) {
    const body = $("trustReceiptBody");
    if (!body) return;
    const row = document.createElement("div");
    row.className = "r-line";
    row.style.animationDelay = "0.04s";
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = line.t != null ? Number(line.t).toFixed(3) : "—";
    const msg = document.createElement("span");
    msg.innerHTML = formatReceiptMessage(line);
    row.appendChild(t);
    row.appendChild(msg);
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  function renderReceipt({ full = false } = {}) {
    const body = $("trustReceiptBody");
    if (!body) return;
    updateReceiptHead();
    if (
      full ||
      !state.receiptLines.length ||
      state.receiptDomCount > state.receiptLines.length
    ) {
      body.innerHTML = "";
      state.receiptDomCount = 0;
    }
    for (let i = state.receiptDomCount; i < state.receiptLines.length; i++) {
      appendReceiptDomLine(state.receiptLines[i]);
    }
    state.receiptDomCount = state.receiptLines.length;

    const total = $("trustReceiptTotal");
    if (total) {
      if (state.receiptHeld) {
        total.hidden = false;
        total.innerHTML = `<span>RECORD BEFORE PROMISE</span><span class="ok">HELD ✓</span>`;
      } else {
        total.hidden = true;
        total.innerHTML = "";
      }
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderLedger() {
    const host = $("trustLedger");
    if (!host) return;
    host.innerHTML = "";
    if (!state.promises.length) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML =
        `<span>future contact promises</span><span class="m">·</span><span class="ok">none ✓</span>`;
      host.appendChild(row);
      return;
    }
    state.promises.forEach((p) => {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span>${escapeHtml(JSON.stringify(p.spoken).slice(1, -1))}</span><span class="m">${escapeHtml(p.recordId)}</span><span class="ok">backed ✓</span>`;
      host.appendChild(row);
    });
    const none = document.createElement("div");
    none.className = "row";
    none.innerHTML =
      `<span>extra contact promises</span><span class="m">·</span><span class="ok">none ✓</span>`;
    host.appendChild(none);
  }

  function renderGates() {
    const p50 = median(state.ttfaSamples);
    const ttfaGate = state.gateTargets.ttfaP50s || GATE_TTFA;
    const intGate = state.gateTargets.interruptMs || GATE_INTERRUPT;
    const turnsGate = state.gateTargets.turnsAfterOptOut || GATE_TURNS;

    setGateMeter(
      "gateTtfa",
      p50 != null ? p50 / 1000 : null,
      ttfaGate,
      (v) => `${Number(v).toFixed(2)} s`,
      (v, lim) => (v / lim) * 80
    );
    setGateMeter(
      "gateInterrupt",
      state.interruptMs,
      intGate,
      (v) => `${Math.round(v)} ms`,
      (v, lim) => (v / lim) * 80
    );
    setGateMeter(
      "gateOptOutTurns",
      state.opt_out_seen ? state.turnsAfterOptOut : null,
      turnsGate,
      (v) => String(v),
      (v, lim) => (lim ? (v / lim) * 50 : 0)
    );
  }

  function setGateMeter(prefix, value, limit, fmt, widthFn) {
    const valEl = $(prefix + "Val");
    const limEl = $(prefix + "Lim");
    const bar = $(prefix + "Bar");
    const mark = $(prefix + "Mark");
    if (limEl) limEl.textContent = `gate ${limit}`;
    if (mark) mark.style.left = "80%";
    if (value == null || Number.isNaN(Number(value))) {
      if (valEl) {
        valEl.textContent = "—";
        valEl.className = "";
      }
      if (bar) {
        bar.style.width = "0%";
        bar.className = "";
      }
      return;
    }
    const ok = Number(value) <= Number(limit);
    if (valEl) {
      valEl.textContent = fmt(value);
      valEl.className = ok ? "g-ok" : "g-warn";
    }
    if (bar) {
      const pct = Math.max(4, Math.min(100, widthFn(Number(value), Number(limit))));
      bar.style.width = `${pct}%`;
      bar.className = ok ? "" : "warn";
    }
  }

  function renderEcon() {
    const totals =
      typeof window.callUsageTotals === "object" && window.callUsageTotals
        ? window.callUsageTotals
        : { costUsd: 0, inputTokens: 0, outputTokens: 0 };
    const turns = document.querySelectorAll(
      "#turnTableBody tr[data-voice-row='1']"
    ).length;
    const big = $("econThisCall");
    if (big) {
      const usd = Number(totals.costUsd) || 0;
      big.innerHTML = `${usd > 0 ? "$" + usd.toFixed(3) : "$—"}<small>THIS CALL · ${turns} TURNS</small>`;
    }
    const tok = $("econTokens");
    if (tok) {
      const inn = Number(totals.inputTokens) || 0;
      const out = Number(totals.outputTokens) || 0;
      const innL = inn >= 1000 ? `${(inn / 1000).toFixed(1)}k` : String(inn || "—");
      const outL = out >= 1000 ? `${(out / 1000).toFixed(1)}k` : String(out || "—");
      tok.innerHTML = `${innL} / ${outL}<small>TOKENS IN / OUT</small>`;
    }
  }

  function renderFreeze() {
    const el = $("intakeFreezeNote");
    if (!el) return;
    if (state.intakeFrozen) {
      el.classList.add("show");
      el.textContent = `🔒 intake frozen the moment the opt out matched · nothing after ${state.freezeAt || "that point"} was stored`;
    } else {
      el.classList.remove("show");
    }
  }

  function renderScorecard() {
    const host = $("scorecardPills");
    const sub = $("scorecardSubtitle");
    if (!host) return;
    host.innerHTML = "";
    if (!liveScorecard) {
      if (sub) sub.textContent = "Loading live evals…";
      const tip = document.createElement("span");
      tip.className = "score-pill dashed";
      tip.textContent = "fetching scorecard…";
      host.appendChild(tip);
      return;
    }
    if (liveScorecard.error) {
      if (sub) {
        sub.textContent =
          liveScorecard.hint ||
          "Eval harness unavailable — rebuild image with server/evals";
      }
      const tip = document.createElement("span");
      tip.className = "score-pill miss";
      tip.innerHTML = `scorecard <b>error</b>`;
      tip.title = liveScorecard.error === true ? "eval harness failed" : String(liveScorecard.error);
      host.appendChild(tip);
      return;
    }
    const n = liveScorecard.scenarioCount || 0;
    const eng = liveScorecard.engine || "fsm";
    if (sub) {
      sub.textContent = `Build suite (not this call) · ${n} scenarios · engine ${eng}`;
    }
    const rows = [
      ...(liveScorecard.categories || []),
      liveScorecard.overall,
    ].filter(Boolean);
    rows.forEach((p) => {
      const span = document.createElement("span");
      span.className = `score-pill${p.miss ? " miss" : ""}`;
      span.innerHTML = `${escapeHtml(p.label)} <b>${p.pass} / ${p.total}</b>`;
      host.appendChild(span);
    });
    const link = document.createElement("button");
    link.type = "button";
    link.className = "score-pill dashed";
    link.textContent = "refresh evals";
    link.title = "Re-run orchestrator text evals";
    link.style.cursor = "pointer";
    link.addEventListener("click", () => loadScorecard({ refresh: true }));
    host.appendChild(link);
  }

  async function loadScorecard({ refresh = false } = {}) {
    const host = $("scorecardPills");
    if (refresh && host) {
      liveScorecard = null;
      renderScorecard();
    }
    try {
      const q = refresh ? "?refresh=1" : "";
      const res = await fetch(`/api/scorecard${q}`, { credentials: "same-origin" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        liveScorecard = {
          error: body.error || true,
          hint: body.hint || "Eval harness unavailable",
        };
      } else if (body && body.error) {
        liveScorecard = {
          error: body.error,
          hint: body.hint || "Eval harness unavailable",
        };
      } else {
        liveScorecard = body;
      }
    } catch (_) {
      liveScorecard = { error: true };
    }
    renderScorecard();
  }

  function renderEngLog() {
    const el = $("trustEngLog");
    if (!el) return;
    if (!state.engLog.length) {
      el.textContent = "Event log will fill as gates and tools fire.";
      return;
    }
    el.innerHTML = state.engLog
      .map((l) => `<b>${escapeHtml(l.t)}</b> ${escapeHtml(l.text)}`)
      .join(" · ");
  }

  function renderAll() {
    renderRibbon();
    renderReceipt();
    renderLedger();
    renderGates();
    renderEcon();
    renderFreeze();
    renderEngLog();
  }

  function pushEng(text) {
    const t = new Date().toLocaleTimeString(undefined, { hour12: false });
    state.engLog.push({ t, text });
    if (state.engLog.length > 40) state.engLog.shift();
    renderEngLog();
  }

  function annotateLastAssistantLatency(ttfaMs) {
    const log = $("transcriptLog");
    if (!log) return;
    const bubbles = log.querySelectorAll(".transcript-bubble.assistant");
    const last = bubbles[bubbles.length - 1];
    if (!last) return;
    let lat = last.querySelector(".lat");
    if (!lat) {
      lat = document.createElement("span");
      lat.className = "lat";
      last.appendChild(lat);
    }
    const s = Number(ttfaMs) / 1000;
    const gate = state.gateTargets.ttfaP50s || GATE_TTFA;
    const ok = s <= gate;
    lat.classList.toggle("warn", !ok);
    lat.textContent = `◔ first sound ${s.toFixed(2)} s · gate ${gate.toFixed(2)} ${ok ? "✓" : "!"}`;
    if (state.lastPromiseId && !last.querySelector(".promiselink")) {
      const a = document.createElement("span");
      a.className = "promiselink";
      a.textContent = `promise backed by record ${state.lastPromiseId}`;
      last.appendChild(a);
    }
    if (state.opt_out_seen && state.turnsAfterOptOut > 0) {
      lat.textContent += ` · ${state.turnsAfterOptOut} turn after opt out ✓`;
    }
  }

  function insertGateBanner(message) {
    const log = $("transcriptLog");
    if (!log || !message) return;
    const div = document.createElement("div");
    div.className = "gatehit";
    div.innerHTML = `<span>${escapeHtml(message)}</span>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function markFrozenUserBubbles() {
    if (!state.intakeFrozen) return;
    const log = $("transcriptLog");
    if (!log) return;
    const users = log.querySelectorAll(".transcript-bubble.user:not(.frozen)");
    // Only mark bubbles that appear after a gatehit.
    let after = false;
    for (const child of log.children) {
      if (child.classList && child.classList.contains("gatehit")) after = true;
      if (
        after &&
        child.classList &&
        child.classList.contains("user") &&
        !child.classList.contains("frozen")
      ) {
        child.classList.add("frozen");
        if (!child.querySelector(".lockline")) {
          const lock = document.createElement("span");
          lock.className = "lockline";
          lock.textContent = `⊘ not captured · intake frozen at ${state.freezeAt || "opt out"}`;
          child.appendChild(lock);
        }
        const lab = child.querySelector(".transcript-label");
        if (lab && !lab.textContent.includes("AFTER")) {
          lab.textContent = "You · after opt out";
        }
      }
    }
  }

  function handleTrustEvent(msg) {
    if (!msg || msg.event !== "trust") return;
    const kind = msg.trustKind || "snapshot";
    if (msg.callShort) state.callShort = msg.callShort;
    if (msg.callId) state.callId = msg.callId;
    if (msg.stage) state.stage = msg.stage;
    if (msg.disposition != null) state.disposition = msg.disposition;

    if (kind === "snapshot") {
      if (Array.isArray(msg.ribbon)) state.ribbon = msg.ribbon;
      if (msg.intakeFrozen != null) state.intakeFrozen = !!msg.intakeFrozen;
      if (msg.gateTargets) state.gateTargets = { ...state.gateTargets, ...msg.gateTargets };
      if (msg.turnsAfterOptOut != null) state.turnsAfterOptOut = msg.turnsAfterOptOut;
      if (msg.lastInterruptMs != null) state.interruptMs = msg.lastInterruptMs;
      renderAll();
      return;
    }

    if (kind === "receipt") {
      if (!state.receiptClock) {
        state.receiptClock = new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
      }
      if (msg.eventLabel) state.receiptEvent = msg.eventLabel;
      if (msg.disposition) state.disposition = msg.disposition;
      state.receiptLines.push({
        t: msg.t,
        text: msg.line,
        ok: !!msg.ok,
        recordId: msg.recordId || null,
        highlight: msg.highlight || null,
      });
      if (msg.intakeFrozen) state.intakeFrozen = true;
      if (msg.turnsAfterOptOut != null) state.turnsAfterOptOut = msg.turnsAfterOptOut;
      renderReceipt();
      renderFreeze();
      renderGates();
      return;
    }

    if (kind === "receipt_total") {
      state.receiptHeld = true;
      renderReceipt();
      return;
    }

    if (kind === "promise") {
      state.promises.push({
        spoken: msg.spoken,
        recordId: msg.recordId,
      });
      state.lastPromiseId = msg.recordId;
      renderLedger();
      return;
    }

    if (kind === "gate_hit") {
      state.opt_out_seen = msg.action === "DNC_CLOSE" || state.opt_out_seen;
      const flushBit = msg.message || `GATE · ${msg.action}`;
      state.pendingGateBanner = flushBit;
      insertGateBanner(flushBit);
      pushEng(flushBit);
      return;
    }

    if (kind === "gate_flush") {
      if (msg.interruptMs != null) state.interruptMs = msg.interruptMs;
      if (state.pendingGateBanner && msg.message) {
        // Enrich last gatehit with stop timing.
        const hits = document.querySelectorAll(".gatehit span");
        const last = hits[hits.length - 1];
        if (last && !last.textContent.includes("stopped")) {
          last.textContent = `${last.textContent} · ${msg.message}`;
        }
        state.pendingGateBanner = null;
      }
      renderGates();
      pushEng(msg.message || `flush ${msg.interruptMs}ms`);
      return;
    }

    if (kind === "freeze") {
      state.intakeFrozen = true;
      state.freezeAt = msg.at || new Date().toLocaleTimeString();
      state.opt_out_seen = true;
      renderFreeze();
      markFrozenUserBubbles();
      return;
    }

    if (kind === "opt_out_turns") {
      if (msg.turnsAfterOptOut != null) state.turnsAfterOptOut = msg.turnsAfterOptOut;
      renderGates();
      return;
    }

    if (kind === "eng_log") {
      pushEng(msg.text || "");
      return;
    }

    if (kind === "silence") {
      state.silence = {
        status: msg.status || "idle",
        nextGapS: msg.nextGapS,
        checkinIndex: msg.checkinIndex,
        checkinsDone: msg.checkinsDone,
      };
      if (msg.status === "checkin") {
        insertGateBanner(
          `SILENCE · check-in ${msg.checkinIndex || ""} · quiet gap elapsed`
        );
      }
      if (msg.status === "close") {
        insertGateBanner("SILENCE · no-response close");
      }
      renderSilenceChip();
      pushEng(
        msg.status === "armed"
          ? `silence armed next=${msg.nextGapS}s`
          : `silence ${msg.status}`
      );
    }
  }

  function onMetrics(msg) {
    if (!msg) return;
    if (msg.event === "first_audio" && msg.ttfaMs != null) {
      state.ttfaSamples.push(Number(msg.ttfaMs));
      annotateLastAssistantLatency(msg.ttfaMs);
      renderGates();
    }
    if (msg.kind === "metrics" && msg.metrics && msg.metrics.ttfaMs != null) {
      const v = Number(msg.metrics.ttfaMs);
      if (!Number.isNaN(v)) {
        // Prefer timeline first_audio; still accept completed turn metrics.
        if (!state.ttfaSamples.length || state.ttfaSamples[state.ttfaSamples.length - 1] !== v) {
          state.ttfaSamples.push(v);
        }
        renderGates();
      }
    }
    renderEcon();
    syncEngTable();
  }

  function syncEngTable() {
    const body = $("trustEngTableBody");
    if (!body) return;
    const rows = document.querySelectorAll("#turnTableBody tr[data-voice-row='1']");
    if (!rows.length) {
      body.innerHTML =
        '<tr><td colspan="6" class="muted" style="text-align:center;">No agent responses yet.</td></tr>';
      return;
    }
    body.innerHTML = "";
    rows.forEach((tr, i) => {
      const cells = tr.querySelectorAll("td");
      const out = document.createElement("tr");
      // Prefer stage stamped on the source row at append time (QUALIFY stays QUALIFY).
      const rowStage = tr.dataset.stage || state.stage || "—";
      const ttfa = cells[4] ? cells[4].textContent : "—";
      const e2e = cells[6] ? cells[6].textContent : "—";
      const tok = cells[8] ? cells[8].textContent : "—";
      const cost = cells[12] ? cells[12].textContent : "—";
      out.innerHTML = `<td>${i + 1}</td><td class="hl">${escapeHtml(rowStage)}</td><td class="ok">${escapeHtml(ttfa)}</td><td>${escapeHtml(e2e)}</td><td>${escapeHtml(tok)}</td><td>${escapeHtml(cost)}</td>`;
      body.appendChild(out);
    });
  }

  async function exportAuditBundle() {
    const turns = [];
    document.querySelectorAll("#transcriptLog .transcript-bubble").forEach((b) => {
      const role = b.classList.contains("user") ? "caller" : "agent";
      const text = (b.querySelector(".transcript-text") || {}).textContent || "";
      turns.push({ role, text });
    });
    const payload = {
      exportedAt: new Date().toISOString(),
      callId: state.callId || window.currentCallId || null,
      callShort: state.callShort,
      stage: state.stage,
      disposition: state.disposition,
      intakeFrozen: state.intakeFrozen,
      receipt: state.receiptLines,
      receiptHeld: state.receiptHeld,
      promises: state.promises,
      gates: {
        ttfaP50Ms: median(state.ttfaSamples),
        interruptMs: state.interruptMs,
        turnsAfterOptOut: state.turnsAfterOptOut,
        targets: state.gateTargets,
      },
      economics: window.callUsageTotals || null,
      transcript: turns,
      engLog: state.engLog,
      scorecardNote: liveScorecard
        ? "Live orchestrator text-eval scorecard at export time."
        : "Scorecard not loaded yet.",
      liveScorecard: liveScorecard,
    };
    let recordingHash = null;
    try {
      const enc = new TextEncoder().encode(JSON.stringify(turns));
      const buf = await crypto.subtle.digest("SHA-256", enc);
      recordingHash = Array.from(new Uint8Array(buf))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
    } catch (_) {
      recordingHash = null;
    }
    payload.transcriptHashSha256 = recordingHash;
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `audit-${state.callShort || "call"}-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function resetTrust() {
    state.callShort = "—";
    state.callId = null;
    state.intakeFrozen = false;
    state.freezeAt = null;
    state.disposition = null;
    state.receiptLines = [];
    state.receiptHeld = null;
    state.receiptEvent = null;
    state.receiptClock = null;
    state.receiptDomCount = 0;
    state.promises = [];
    state.interruptMs = null;
    state.turnsAfterOptOut = 0;
    state.ttfaSamples = [];
    state.engLog = [];
    state.pendingGateBanner = null;
    state.lastPromiseId = null;
    state.opt_out_seen = false;
    state.silence = { status: "idle" };
    setPreCallRibbon();
    renderAll();
    renderScorecard();
    syncEngTable();
  }

  /** Call is live — highlight INTRO until/unless a trust snapshot overrides. */
  function onCallStarted() {
    if (state.stage === "CLASSIFY") {
      state.stage = "INTRO";
      state.ribbon = [
        { id: "classify", label: "CLASSIFY · human", status: "done" },
        { id: "intro", label: "INTRO", status: "active" },
        { id: "qualify", label: "QUALIFY", status: "pending" },
      ];
      renderRibbon();
    }
  }

  // Observe frozen bubbles after new user turns.
  const _obsTarget = () => $("transcriptLog");
  function watchTranscript() {
    const log = _obsTarget();
    if (!log || log._trustObserved) return;
    log._trustObserved = true;
    const mo = new MutationObserver(() => {
      if (state.intakeFrozen) markFrozenUserBubbles();
      // Tag assistant labels with stage
      log.querySelectorAll(".transcript-bubble.assistant .transcript-label").forEach((lab) => {
        if (lab.dataset.staged) return;
        lab.dataset.staged = "1";
        lab.textContent = `Agent · ${state.stage}`;
      });
    });
    mo.observe(log, { childList: true, subtree: true });
  }

  function wireEconAccordion() {
    const panel = $("econPanel");
    const btn = $("econToggle");
    if (!panel || !btn || btn._econWired) return;
    btn._econWired = true;
    btn.addEventListener("click", () => {
      const open = !panel.classList.contains("open");
      panel.classList.toggle("open", open);
      btn.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  window.TrustConsole = {
    setView,
    handleTrustEvent,
    onMetrics,
    exportAuditBundle,
    reset: resetTrust,
    setPreCallRibbon,
    onCallStarted,
    renderEcon,
    syncEngTable,
    state,
  };

  document.addEventListener("DOMContentLoaded", () => {
    setView(false);
    setPreCallRibbon();
    renderScorecard();
    loadScorecard();
    renderAll();
    watchTranscript();
    wireEconAccordion();
    const exp = $("trustExportBtn");
    if (exp) exp.addEventListener("click", () => exportAuditBundle());
  });
})();
