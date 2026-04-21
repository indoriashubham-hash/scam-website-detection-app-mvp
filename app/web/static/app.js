// Shared helpers for the Website Risk Investigator UI.
//
// Exposes a single global object `wri` with rendering utilities used by
// both index.html and investigation.html. Kept vanilla on purpose — no
// framework, no build step, no npm. If you're adding something here, ask
// yourself if it belongs in the API response instead.

(function () {
  "use strict";

  // ----- escaping ---------------------------------------------------------
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ----- chip / badge classes --------------------------------------------
  // Keep these aligned with the CSS in styles.css; changing a name in one
  // place and not the other is the classic way this gets silently wrong.
  function chipClass(band) {
    switch ((band || "").toLowerCase()) {
      case "critical": return "chip-critical";
      case "high": return "chip-high";
      case "medium": return "chip-medium";
      case "low": return "chip-low";
      case "none": return "chip-none";
      case "insufficient": return "chip-insufficient";
      default: return "chip-unknown";
    }
  }

  function severityChipClass(sev) {
    switch ((sev || "").toLowerCase()) {
      case "critical": return "sev sev-critical";
      case "high": return "sev sev-high";
      case "medium": return "sev sev-medium";
      case "low": return "sev sev-low";
      case "info": return "sev sev-info";
      default: return "sev sev-info";
    }
  }

  function statusBadge(status) {
    const s = (status || "").toLowerCase();
    const cls =
      s === "done" ? "s-done" :
      s === "failed" ? "s-failed" :
      s === "queued" ? "s-queued" : "s-running";
    return `<span class="status ${cls}">${esc(s || "unknown")}</span>`;
  }

  // ----- recent-investigations card (home page) --------------------------
  function recentCard(inv) {
    const a = document.createElement("a");
    a.className = "recent-card";
    a.href = `/i/${inv.id}`;

    const left = document.createElement("div");
    left.className = "recent-left";
    const chip = document.createElement("span");
    chip.className = "chip " + chipClass(inv.risk_band);
    chip.textContent = (inv.risk_band || "pending").toUpperCase();
    left.appendChild(chip);

    const mid = document.createElement("div");
    mid.className = "recent-mid";
    const urlLine = document.createElement("div");
    urlLine.className = "recent-url";
    urlLine.textContent = inv.input_url;
    mid.appendChild(urlLine);

    const sumLine = document.createElement("div");
    sumLine.className = "recent-summary muted small";
    const headline = (inv.narrative && inv.narrative.headline) || inv.summary || "";
    sumLine.textContent = headline;
    mid.appendChild(sumLine);

    const right = document.createElement("div");
    right.className = "recent-right small muted";
    right.innerHTML = statusBadge(inv.status) +
      "<br>" +
      esc(new Date(inv.created_at).toLocaleString());

    a.appendChild(left);
    a.appendChild(mid);
    a.appendChild(right);
    return a;
  }

  // ----- findings list (detail page) -------------------------------------
  function renderFindings(findings, host) {
    host.innerHTML = "";
    if (!findings.length) {
      host.innerHTML = '<p class="muted small">No findings recorded.</p>';
      return;
    }
    for (const f of findings) {
      const row = document.createElement("div");
      row.className = "finding";
      row.innerHTML = `
        <div class="finding-top">
          <span class="${severityChipClass(f.severity)}">${esc(f.severity)}</span>
          <code class="kind">${esc(f.kind)}</code>
          <span class="small muted">conf ${Math.round((f.confidence || 0) * 100)}%</span>
        </div>
        <div class="finding-summary">${esc(f.summary || "")}</div>
      `;
      host.appendChild(row);
    }
  }

  // ----- pages list (detail page) ----------------------------------------
  function renderPages(pages, host) {
    host.innerHTML = "";
    if (!pages.length) {
      host.innerHTML = '<p class="muted small">No pages were crawled.</p>';
      return;
    }
    for (const p of pages) {
      const row = document.createElement("div");
      row.className = "page-row";
      const role = p.is_seed ? "SEED" : p.is_homepage_compare ? "HOMEPAGE" : "";
      const thumb = p.atf_screenshot_url || p.screenshot_url || "";
      row.innerHTML = `
        <div class="page-thumb">
          ${thumb ? `<a href="${esc(thumb)}" target="_blank"><img src="${esc(thumb)}" alt="screenshot"></a>` : "<div class='thumb-empty'>no screenshot</div>"}
        </div>
        <div class="page-meta">
          ${role ? `<span class="role-tag role-${role.toLowerCase()}">${role}</span>` : ""}
          <div class="page-title">${esc(p.title || "(untitled)")}</div>
          <div class="page-url small muted">${esc(p.final_url || p.url)}</div>
          <div class="small muted">HTTP ${esc(p.http_status ?? "?")} · lang ${esc(p.lang || "?")} · ${esc(p.word_count ?? 0)} words</div>
        </div>
      `;
      host.appendChild(row);
    }
  }

  // ----- deep review rendering (Track 2, Minto pyramid shape) ------------
  function renderDeepReview(dr) {
    const root = document.createElement("div");
    if (!dr) {
      root.innerHTML = '<p class="muted small">No deep review available.</p>';
      return root;
    }

    // Shape detection. Current schema (v2) has `governing_thought` and
    // `supporting_pillars`. The old (v1) shape had `summary` +
    // `observations/concerns/inconsistencies/positive_indicators`. We keep
    // the legacy renderer so an older cached review still displays; fresh
    // reviews come back in v2.
    const isV2 =
      typeof dr.governing_thought === "string" ||
      Array.isArray(dr.supporting_pillars);

    if (isV2) {
      renderDeepReviewV2(dr, root);
    } else {
      renderDeepReviewLegacy(dr, root);
    }

    const meta = document.createElement("p");
    meta.className = "small muted deep-meta";
    meta.textContent = `Generated by ${dr.model || "LLM"} · every claim above cites an evidence source.`;
    root.appendChild(meta);
    return root;
  }

  function renderSources(sources) {
    // Handles both "sources: [...]" (v2 array) and "source: '...'" (v1 scalar).
    const list = Array.isArray(sources) ? sources : sources ? [sources] : [];
    if (!list.length) return "";
    return (
      '<div class="deep-item-source small muted">' +
      (list.length === 1 ? "source: " : "sources: ") +
      list.map((s) => `<code>${esc(s)}</code>`).join(" · ") +
      "</div>"
    );
  }

  function renderSourcedList(title, cls, items, host) {
    if (!items || !items.length) return;
    const h = document.createElement("h3");
    h.className = "deep-heading " + cls;
    h.textContent = title;
    host.appendChild(h);
    const ul = document.createElement("ul");
    ul.className = "deep-list";
    for (const it of items) {
      const li = document.createElement("li");
      if (typeof it === "string") {
        li.textContent = it;
      } else {
        const sources = it.sources != null ? it.sources : it.source;
        li.innerHTML =
          `<div class="deep-item-text">${esc(it.text)}</div>` +
          renderSources(sources);
      }
      ul.appendChild(li);
    }
    host.appendChild(ul);
  }

  function renderDeepReviewV2(dr, root) {
    // Governing thought — the Minto answer-first line.
    if (dr.governing_thought) {
      const gov = document.createElement("p");
      gov.className = "deep-governing";
      gov.textContent = dr.governing_thought;
      root.appendChild(gov);
    }

    // Supporting pillars — MECE reasons the governing thought holds.
    if (Array.isArray(dr.supporting_pillars) && dr.supporting_pillars.length) {
      const section = document.createElement("div");
      section.className = "deep-pillars";
      for (let i = 0; i < dr.supporting_pillars.length; i++) {
        const p = dr.supporting_pillars[i] || {};
        const card = document.createElement("div");
        card.className = "pillar-card";

        const head = document.createElement("div");
        head.className = "pillar-head";
        const n = document.createElement("span");
        n.className = "pillar-number";
        n.textContent = String(i + 1);
        const claim = document.createElement("div");
        claim.className = "pillar-claim";
        claim.textContent = p.claim || "";
        head.appendChild(n);
        head.appendChild(claim);
        card.appendChild(head);

        if (Array.isArray(p.evidence) && p.evidence.length) {
          const ul = document.createElement("ul");
          ul.className = "pillar-evidence";
          for (const e of p.evidence) {
            const li = document.createElement("li");
            const sources = e.sources != null ? e.sources : e.source;
            li.innerHTML =
              `<div class="deep-item-text">${esc(e.text || "")}</div>` +
              renderSources(sources);
            ul.appendChild(li);
          }
          card.appendChild(ul);
        }
        section.appendChild(card);
      }
      root.appendChild(section);
    }

    renderSourcedList("Contradictions", "deep-h-contradictions", dr.contradictions, root);
    renderSourcedList("Caveats", "deep-h-caveats", dr.caveats, root);
  }

  function renderDeepReviewLegacy(dr, root) {
    // Older reviews (schema v1) still linger in Postgres until the user
    // regenerates. Render them with the original layout so they don't look
    // broken.
    if (dr.summary) {
      const p = document.createElement("p");
      p.className = "deep-summary";
      p.textContent = dr.summary;
      root.appendChild(p);
    }
    renderSourcedList("Observations", "deep-h-observations", dr.observations, root);
    renderSourcedList("Concerns", "deep-h-concerns", dr.concerns, root);
    renderSourcedList("Inconsistencies", "deep-h-inconsistencies", dr.inconsistencies, root);
    renderSourcedList("Positive indicators", "deep-h-positive", dr.positive_indicators, root);
    renderSourcedList("Caveats", "deep-h-caveats", dr.caveats, root);
  }

  // ----- BYOK: API key management ----------------------------------------
  // Store the user's Anthropic key in localStorage ONLY. Never cookies, never
  // sent anywhere except with the explicit POST bodies below. The server
  // doesn't persist it; it just forwards to the Anthropic SDK once per call.
  const KEY_STORAGE = "wri.anthropicKey";

  function getApiKey() {
    try {
      return localStorage.getItem(KEY_STORAGE) || "";
    } catch (_) {
      return "";
    }
  }

  function setApiKey(value) {
    try {
      if (value) {
        localStorage.setItem(KEY_STORAGE, value);
      } else {
        localStorage.removeItem(KEY_STORAGE);
      }
    } catch (_) { /* quota / privacy mode — silently ignore */ }
  }

  function maskKey(key) {
    if (!key) return "(not set)";
    if (key.length <= 10) return "•••••";
    return key.slice(0, 7) + "…" + key.slice(-4);
  }

  // Render the key-management widget into a container element. Same markup
  // works on the home page and the investigation page; callers just pick a
  // host element and pass it here.
  function renderKeyWidget(host) {
    host.innerHTML = "";
    host.classList.add("key-widget");

    const row = document.createElement("div");
    row.className = "key-row";

    const label = document.createElement("span");
    label.className = "small muted";
    label.textContent = "Anthropic API key:";
    row.appendChild(label);

    const display = document.createElement("code");
    display.className = "key-display";
    display.textContent = maskKey(getApiKey());
    row.appendChild(display);

    const editBtn = document.createElement("button");
    editBtn.className = "link-btn";
    editBtn.type = "button";
    editBtn.textContent = getApiKey() ? "Change" : "Add";
    row.appendChild(editBtn);

    const clearBtn = document.createElement("button");
    clearBtn.className = "link-btn";
    clearBtn.type = "button";
    clearBtn.textContent = "Clear";
    if (!getApiKey()) clearBtn.classList.add("hidden");
    row.appendChild(clearBtn);

    host.appendChild(row);

    const editor = document.createElement("div");
    editor.className = "key-editor hidden";
    const input = document.createElement("input");
    input.type = "password";
    input.placeholder = "sk-ant-…";
    input.autocomplete = "off";
    input.spellcheck = false;
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = "Save";
    saveBtn.className = "primary";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "link-btn";
    cancelBtn.textContent = "Cancel";
    editor.appendChild(input);
    editor.appendChild(saveBtn);
    editor.appendChild(cancelBtn);
    host.appendChild(editor);

    const note = document.createElement("div");
    note.className = "small muted key-note";
    note.textContent =
      "Stored in this browser only. Sent with each request and never persisted server-side.";
    host.appendChild(note);

    function refresh() {
      display.textContent = maskKey(getApiKey());
      editBtn.textContent = getApiKey() ? "Change" : "Add";
      clearBtn.classList.toggle("hidden", !getApiKey());
    }

    editBtn.addEventListener("click", () => {
      editor.classList.remove("hidden");
      input.value = "";
      input.focus();
    });
    cancelBtn.addEventListener("click", () => {
      editor.classList.add("hidden");
    });
    saveBtn.addEventListener("click", () => {
      const v = input.value.trim();
      if (!v) return;
      setApiKey(v);
      editor.classList.add("hidden");
      refresh();
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        saveBtn.click();
      }
    });
    clearBtn.addEventListener("click", () => {
      setApiKey("");
      refresh();
    });
  }

  window.wri = {
    esc,
    chipClass,
    severityChipClass,
    statusBadge,
    recentCard,
    renderFindings,
    renderPages,
    renderDeepReview,
    getApiKey,
    setApiKey,
    renderKeyWidget,
  };
})();
