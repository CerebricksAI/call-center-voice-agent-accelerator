/* Shared Settings modal — per-session voice-model selection.
 *
 * Loaded on every page (Pre-Qual, Call History, Analytics) so the Settings link
 * works everywhere. The choice is persisted in localStorage, sent as ?model= on
 * the call WebSocket (via window.effectiveVoiceModel), and recorded with each call
 * server-side. Badge/transcript labels update only on pages where those elements
 * exist (the Pre-Qual page). No latency/cost technical copy is shown in the modal.
 */
(function () {
  "use strict";

  var KEY = "voiceModel";
  var models = { selectable: [], default: "", transcription: "", extract: "" };

  function getSelected() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }
  function effective() {
    var sel = getSelected();
    if (sel && (!models.selectable.length || models.selectable.indexOf(sel) !== -1)) return sel;
    return models.default || models.selectable[0] || "";
  }
  window.getSelectedVoiceModel = getSelected;
  window.effectiveVoiceModel = effective;

  // ---- optional custom system prompt (blank = built-in persona) ----
  var PROMPT_KEY = "systemPrompt";
  function getSystemPrompt() {
    try { return (localStorage.getItem(PROMPT_KEY) || "").trim(); } catch (e) { return ""; }
  }
  function b64urlEncode(str) {
    var bytes = new TextEncoder().encode(str);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  window.effectiveSystemPrompt = getSystemPrompt;
  // base64url-encoded prompt for the WS connect query (or "" when none set).
  window.effectiveSystemPromptEncoded = function () {
    var p = getSystemPrompt();
    return p ? b64urlEncode(p) : "";
  };

  // ---- styles (injected once) ----
  if (!document.getElementById("settings-modal-styles")) {
    var style = document.createElement("style");
    style.id = "settings-modal-styles";
    style.textContent = [
      ".settings-modal{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center}",
      ".settings-modal[hidden]{display:none}",
      ".settings-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.45)}",
      ".settings-card{position:relative;width:min(860px,94vw);max-height:min(92vh,900px);display:flex;flex-direction:column;background:var(--bg-deep,#fff);color:var(--text-primary,#111);border:1px solid var(--qt-border,#ddd);border-radius:12px;box-shadow:0 18px 48px rgba(0,0,0,.28);overflow:hidden}",
      ".settings-head{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:1px solid var(--qt-border,#eee);flex-shrink:0}",
      ".settings-head h2{margin:0;font-size:1.05rem}",
      ".settings-close{background:none;border:none;font-size:1.5rem;line-height:1;cursor:pointer;color:var(--text-secondary,#666)}",
      ".settings-body{padding:1.25rem 1.5rem;overflow-y:auto;flex:1;min-height:0}",
      ".settings-label{display:block;font-weight:600;font-size:.85rem;margin-bottom:.4rem;color:var(--text-primary,#111)}",
      ".settings-select{width:100%;max-width:100%;padding:.6rem 2.25rem .6rem .75rem;font-size:.95rem;font-weight:500;line-height:1.35;border:1px solid var(--qt-border,#ccc);border-radius:8px;background:var(--bg-elevated,#f8fafc);color:var(--text-primary,#111);cursor:pointer;appearance:none;-webkit-appearance:none;background-image:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%235c6678' d='M2.5 4.5 6 8l3.5-3.5'/%3E%3C/svg%3E\");background-repeat:no-repeat;background-position:right .75rem center;background-size:12px 12px}",
      ".settings-select:hover{border-color:var(--qt-primary,#8929fe)}",
      ".settings-select:focus{outline:2px solid var(--qt-primary,#8929fe);outline-offset:2px;border-color:var(--qt-primary,#8929fe)}",
      ".settings-select option{background:#fff;color:#111827}",
      ".settings-textarea{width:100%;min-height:14rem;margin-top:.25rem;padding:.65rem .75rem;font-size:.875rem;line-height:1.5;border:1px solid var(--qt-border,#ccc);border-radius:8px;background:var(--bg-deep,#fff);color:inherit;resize:vertical;font-family:inherit;box-sizing:border-box}",
      ".settings-note{margin:.9rem 0 0;font-size:.74rem;color:var(--text-muted,#888);line-height:1.5}",
      ".settings-foot{display:flex;justify-content:flex-end;gap:.6rem;padding:1rem 1.25rem;border-top:1px solid var(--qt-border,#eee);flex-shrink:0}",
      ".settings-cancel,.settings-save{padding:.5rem 1rem;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;border:1px solid var(--qt-border,#ccc);background:transparent;color:inherit}",
      ".settings-save{background:var(--qt-primary,#8929fe);border-color:var(--qt-primary,#8929fe);color:#fff}"
    ].join("");
    document.head.appendChild(style);
  }

  // ---- modal markup (injected once) ----
  function ensureModal() {
    if (document.getElementById("settingsModal")) return;
    var wrap = document.createElement("div");
    wrap.innerHTML =
      '<div id="settingsModal" class="settings-modal" hidden role="dialog" aria-modal="true" aria-labelledby="settingsTitle">' +
        '<div class="settings-backdrop"></div>' +
        '<div class="settings-card">' +
          '<div class="settings-head"><h2 id="settingsTitle">Settings</h2>' +
            '<button type="button" class="settings-close" aria-label="Close settings">&times;</button></div>' +
          '<div class="settings-body">' +
            '<label class="settings-label" for="voiceModelSelect">Voice agent model</label>' +
            '<select id="voiceModelSelect" class="settings-select"></select>' +
            '<p class="settings-note">Applies to your next call. The selected model is recorded with each call and shown in Call History.</p>' +
            '<label class="settings-label" for="systemPromptInput" style="margin-top:1rem">Custom system prompt (optional)</label>' +
            '<textarea id="systemPromptInput" class="settings-textarea" rows="12" maxlength="8000" placeholder="Leave blank to use the built-in agent persona. If you enter instructions here, they fully replace how the agent behaves for your next call."></textarea>' +
            '<p class="settings-note">Optional. Blank = built-in persona. If provided, it overrides the agent\'s behaviour/context for your next call (max 8000 chars).</p>' +
          '</div>' +
          '<div class="settings-foot">' +
            '<button type="button" class="settings-cancel">Cancel</button>' +
            '<button type="button" class="settings-save">Save</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    var modal = wrap.firstChild;
    document.body.appendChild(modal);
    modal.querySelector(".settings-backdrop").addEventListener("click", closeSettings);
    modal.querySelector(".settings-close").addEventListener("click", closeSettings);
    modal.querySelector(".settings-cancel").addEventListener("click", closeSettings);
    modal.querySelector(".settings-save").addEventListener("click", saveSettings);
  }

  function populate() {
    var pa = document.getElementById("systemPromptInput");
    if (pa) pa.value = getSystemPrompt();
    var sel = document.getElementById("voiceModelSelect");
    if (!sel) return;
    var current = effective();
    var list = models.selectable.length ? models.selectable : (current ? [current] : []);
    sel.innerHTML = list.map(function (m) {
      return '<option value="' + m + '"' + (m === current ? " selected" : "") + ">" +
        m + (m === models.default ? " (default)" : "") + "</option>";
    }).join("");
  }

  function openSettings(e) {
    if (e && e.preventDefault) e.preventDefault();
    ensureModal();
    populate();
    var modal = document.getElementById("settingsModal");
    if (modal) modal.hidden = false;
  }
  function closeSettings() {
    var modal = document.getElementById("settingsModal");
    if (modal) modal.hidden = true;
  }
  function saveSettings() {
    var sel = document.getElementById("voiceModelSelect");
    if (sel && sel.value) {
      try { localStorage.setItem(KEY, sel.value); } catch (e) {}
    }
    var pa = document.getElementById("systemPromptInput");
    if (pa) {
      var val = (pa.value || "").trim();
      try {
        if (val) localStorage.setItem(PROMPT_KEY, val);
        else localStorage.removeItem(PROMPT_KEY);
      } catch (e) {}
    }
    closeSettings();
    updateBadges();
  }
  window.openSettings = openSettings;
  window.closeSettings = closeSettings;
  window.saveSettings = saveSettings;

  // ---- label/badge updates (no-op where the element is absent) ----
  function updateBadges() {
    var active = effective();
    if (!active) return;
    var badge = document.getElementById("navModelBadge");
    if (badge) {
      badge.textContent = active;
      badge.hidden = false;
      var parts = ["Voice agent model: " + active];
      if (active !== models.default) parts.push("your selection");
      badge.title = parts.join(" · ");
    }
    var tag = document.getElementById("transcriptModelTag");
    if (tag) {
      var transcript = models.transcription ? " · transcript: " + models.transcription : "";
      tag.textContent = " (agent: " + active + transcript + ")";
      tag.title = "Voice agent: " + active +
        (models.transcription ? ". Transcript model (STT): " + models.transcription + "." : "");
    }
    var ex = document.getElementById("extractModelTag");
    if (ex && models.extract) {
      ex.textContent = " (" + models.extract + ")";
      ex.title = "Key-details extraction model: " + models.extract;
    }
  }

  window.loadModelLabels = async function () {
    try {
      var res = await fetch("/api/models", { credentials: "same-origin" });
      if (!res.ok) return;
      var m = await res.json();
      models.selectable = Array.isArray(m.selectableModels) ? m.selectableModels : [];
      models.default = (m.defaultVoiceModel || m.voiceModel || "").trim();
      models.transcription = (m.transcriptionModel || "").trim();
      models.extract = (m.extractModel || "").trim();
      updateBadges();
    } catch (e) {
      /* non-fatal */
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", window.loadModelLabels);
  } else {
    window.loadModelLabels();
  }
})();
