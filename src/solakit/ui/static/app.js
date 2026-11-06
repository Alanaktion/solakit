/* UI behavior only. Credentials never enter browser storage or application JS. */
(() => {
  let started = 0;
  let timer;
  let events;
  const form = () => document.getElementById("composer");
  function jobSchedule() {
    const kind = document.getElementById("job-schedule-kind");
    if (!kind) return;
    const once = kind.value === "once";
    for (const [id, visible] of [["job-cron-fields", !once], ["job-once-fields", once], ["job-cron-options", !once]]) {
      const group = document.getElementById(id);
      group.hidden = !visible;
      group.querySelectorAll("input").forEach(input => {
        input.disabled = !visible;
        if (input.type !== "checkbox") input.required = visible;
      });
    }
  }
  function jobTools() {
    const mode = document.getElementById("job-tools-mode");
    const profile = document.getElementById("job-profile");
    const options = document.getElementById("job-tool-options");
    if (!mode || !profile || !options) return;
    const selected = mode.value === "selected";
    options.hidden = !selected;
    options.querySelectorAll("[data-job-tool]").forEach(label => {
      const input = label.querySelector("input");
      const granted = label.dataset.profiles.split(" ").includes(profile.value);
      label.hidden = !granted && !input.checked;
      input.disabled = !selected || (!granted && !input.checked);
      label.querySelector("[data-tool-warning]").hidden = granted || !input.checked;
    });
  }
  document.addEventListener("change", event => {
    if (event.target.id === "select-session-page") {
      document.querySelectorAll('input[name="session_ids"]').forEach(input => {
        input.checked = event.target.checked;
      });
    }
    if (event.target.id === "job-schedule-kind") jobSchedule();
    if (["job-tools-mode", "job-profile"].includes(event.target.id) || event.target.name === "tools") jobTools();
  });
  function dates(root = document) {
    root.querySelectorAll("time[datetime]").forEach((node) => {
      const date = new Date(node.dateTime);
      if (!Number.isNaN(date.valueOf())) {
        node.title = node.dateTime;
        node.textContent = new Intl.DateTimeFormat(undefined, {
          year: "numeric", month: "short", day: "numeric",
          ...(node.hasAttribute("data-time") ? {hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short"} : {}),
        }).format(date);
      }
    });
  }
  function scrollConversation() {
    const conversation = document.getElementById("conversation");
    if (conversation) conversation.scrollTop = conversation.scrollHeight;
  }
  function waiting(active, createdAt) {
    const pending = document.getElementById("pending");
    const composer = form();
    if (!pending || !composer) return;
    clearInterval(timer);
    composer.setAttribute("aria-busy", String(active));
    composer.querySelectorAll('button[type="submit"]').forEach((node) => { node.disabled = active; });
    pending.hidden = !active;
    if (active) {
      const seed = Date.parse(createdAt || composer.dataset.startedAt || "");
      started = Number.isFinite(seed) ? seed : (started || Date.now());
      composer.dataset.startedAt = new Date(started).toISOString();
      const tick = () => {
        const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
        document.getElementById("elapsed").textContent =
          `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      };
      tick();
      timer = setInterval(tick, 1000);
    } else { started = 0; delete composer.dataset.startedAt; }
  }
  document.addEventListener("htmx:afterSwap", (event) => dates(event.detail.target));
  // Match stable keys inside the active turn. Settled turns are never visited.
  function reconcile(node, incoming) {
    if (node.isEqualNode(incoming)) return;
    if (node.nodeType === 3) { node.nodeValue = incoming.nodeValue; return; }
    for (const attr of [...node.attributes]) {
      if (!incoming.hasAttribute(attr.name) && attr.name !== "open" && attr.name !== "id") node.removeAttribute(attr.name);
    }
    for (const attr of [...incoming.attributes]) {
      if (attr.name !== "open" && node.getAttribute(attr.name) !== attr.value) node.setAttribute(attr.name, attr.value);
    }
    let cursor = node.firstChild;
    for (const next of [...incoming.childNodes]) {
      const key = next.nodeType === 1 ? next.getAttribute("data-key") : null;
      let match = key ? [...node.children].find(child => child.getAttribute("data-key") === key) : cursor;
      if (match && (match.nodeType !== next.nodeType || match.nodeName !== next.nodeName ||
          (!key && match.nodeType === 1 && match.hasAttribute("data-key")))) match = null;
      if (!match) { match = next.cloneNode(true); node.insertBefore(match, cursor); }
      else { if (match !== cursor) node.insertBefore(match, cursor); reconcile(match, next); }
      cursor = match.nextSibling;
    }
    while (cursor) { const next = cursor.nextSibling; cursor.remove(); cursor = next; }
  }
  function draft(save = true) {
    const composer = form(), field = composer?.querySelector("textarea");
    if (!field || !composer.dataset?.draftKey) return;
    try {
      if (save) {
        if (field.value) sessionStorage.setItem(composer.dataset.draftKey, field.value);
        else sessionStorage.removeItem(composer.dataset.draftKey);
      } else field.value = sessionStorage.getItem(composer.dataset.draftKey) || field.value;
    } catch { /* Storage may be disabled; editing still works. */ }
  }
  function grow(field) {
    field.style.height = "auto";
    field.style.height = field.scrollHeight + "px";
  }
  document.addEventListener("input", event => {
    if (event.target.id === "message") { draft(); grow(event.target); }
  });
  document.addEventListener("click", async event => {
    const button = event.target.closest?.(".load-earlier");
    if (!button) return;
    event.preventDefault();
    if (button.getAttribute("aria-busy") === "true") return;
    const conversation = document.getElementById("conversation");
    const anchor = button.nextElementSibling;
    const top = anchor?.getBoundingClientRect().top;
    button.setAttribute("aria-busy", "true");
    try {
      const url = new URL(button.dataset.url, location.href);
      url.searchParams.set("before", button.dataset.before);
      const response = await fetch(url, {credentials: "same-origin"});
      if (!response.ok) throw new Error("History unavailable");
      const result = await response.json();
      const template = document.createElement("template");
      template.innerHTML = result.html;
      button.after(template.content);
      dates(conversation);
      if (anchor) conversation.scrollTop += anchor.getBoundingClientRect().top - top;
      if (result.before) {
        button.dataset.before = result.before;
        button.href = "?before=" + encodeURIComponent(result.before);
      } else button.remove();
    } catch { button.textContent = "Could not load history · try again"; }
    finally { button.removeAttribute("aria-busy"); }
  });
  function streamConversation(runId, createdAt) {
    const conversation = document.getElementById("conversation");
    if (!form() || !conversation?.dataset.eventsUrl || typeof EventSource === "undefined") return;
    events?.close();
    const pending = conversation.querySelector('[data-run-status="queued"], [data-run-status="running"]');
    waiting(true, createdAt || conversation.dataset.activeCreatedAt || pending?.dataset.createdAt);
    const identity = runId || conversation.dataset.activeRun || pending?.dataset.runId;
    const url = conversation.dataset.eventsUrl + (identity ? "?run=" + encodeURIComponent(identity) : "");
    const source = new EventSource(url);
    events = source;
    source.addEventListener("snapshot", (event) => {
      if (source !== events) return;
      const snapshot = JSON.parse(event.data);
      const nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
      const template = document.createElement("template");
      template.innerHTML = snapshot.html; // Authenticated, escaped server templates.
      const incoming = template.content.querySelector("[data-run-id]");
      const current = [...conversation.querySelectorAll("[data-run-id]")].find(node => node.dataset.runId === snapshot.run_id);
      if (incoming) {
        if (current) reconcile(current, incoming);
        else { conversation.querySelector(".conversation-empty")?.remove(); conversation.append(incoming); }
        dates(current || incoming);
      }
      if (snapshot.pending) {
        conversation.dataset.activeRun = snapshot.run_id;
        conversation.dataset.activeCreatedAt = snapshot.created_at;
        waiting(true, snapshot.created_at);
      }
      if (nearBottom) scrollConversation();
      if (!snapshot.pending) {
        source.close();
        events = null;
        waiting(false);
        delete conversation.dataset.activeRun;
        delete conversation.dataset.activeCreatedAt;
        if (document.activeElement === document.body || document.activeElement === form()?.querySelector('button[type="submit"]')) form()?.querySelector("textarea")?.focus();
      }
    });
    const unavailable = () => {
      if (source !== events) return;
      source.close();
      events = null;
      waiting(true);
      document.getElementById("connection-error").hidden = false;
    };
    source.addEventListener("unavailable", unavailable);
    source.addEventListener("expired", () => {
      if (source === events) { draft(); source.close(); events = null; window.location.assign("/login"); }
    });
    source.onerror = unavailable; // No automatic POST retry or duplicate submission.
  }
  document.addEventListener("submit", async (event) => {
    const composer = form();
    if (event.target !== composer) {
      const target = event.target;
      if (target.method?.toLowerCase() !== "post") return;
      if (target.getAttribute("aria-busy") === "true") { event.preventDefault(); return; }
      target.setAttribute("aria-busy", "true");
      // Delay disabling until native form serialization includes the submitter.
      setTimeout(() => target.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(button => { button.disabled = true; }), 0);
      return;
    }
    if (typeof EventSource === "undefined" || typeof fetch === "undefined") return;
    event.preventDefault();
    if (composer.getAttribute("aria-busy") === "true") return;
    const field = composer.querySelector("textarea");
    const sent = field.value;
    const body = new URLSearchParams({message: sent});
    document.getElementById("connection-error").hidden = true;
    waiting(true);
    try {
      const response = await fetch(composer.action, {
        method: "POST", body, headers: {Accept: "application/json"},
        credentials: "same-origin",
      });
      if (response.status === 401) { window.location.assign("/login"); return; }
      if (response.status !== 202) throw new Error("Submission failed");
      const accepted = await response.json();
      // Keep edits made while the acceptance request was in flight.
      if (field.value === sent) field.value = "";
      draft(); grow(field);
      streamConversation(accepted.run_id, accepted.created_at);
    } catch {
      waiting(false);
      document.getElementById("connection-error").hidden = false;
    }
  });
  function positionAnchor() {
    if (!globalThis.location?.hash) return;
    const node = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const conversation = document.getElementById("conversation");
    if (!node || !conversation?.contains(node)) return;
    // Native fragment navigation can also scroll overflow-hidden layout ancestors.
    // Keep the shell fixed and move only the history's own scroll viewport.
    for (let parent = conversation.parentElement; parent; parent = parent.parentElement) parent.scrollTop = 0;
    conversation.scrollTop += node.getBoundingClientRect().top - conversation.getBoundingClientRect().top - (conversation.clientHeight - node.getBoundingClientRect().height) / 2;
  }
  globalThis.addEventListener?.("load", () => requestAnimationFrame(positionAnchor));
  globalThis.addEventListener?.("hashchange", () => landAnchor());
  async function landAnchor() {
    if (!globalThis.location?.hash) { scrollConversation(); return; }
    const anchor = decodeURIComponent(location.hash.slice(1));
    const conversation = document.getElementById("conversation");
    if (!conversation || !/^(message-|turn-)/.test(anchor)) return;
    if (!document.getElementById(anchor)) {
      try {
        const url = new URL(conversation.dataset.historyUrl, location.href);
        url.searchParams.set("anchor", anchor);
        const response = await fetch(url, {credentials: "same-origin"});
        if (!response.ok) return;
        const result = await response.json();
        const template = document.createElement("template");
        template.innerHTML = result.html;
        conversation.replaceChildren(template.content);
        if (result.before) {
          const earlier = document.createElement("a");
          earlier.className = "secondary-button load-earlier";
          earlier.textContent = "Load earlier messages";
          earlier.dataset.before = result.before;
          earlier.dataset.url = conversation.dataset.historyUrl;
          earlier.href = "?before=" + encodeURIComponent(result.before);
          conversation.prepend(earlier);
        }
        if (result.later) {
          const latest = document.createElement("a");
          latest.className = "text-link";
          latest.href = location.pathname;
          latest.textContent = "Return to latest messages";
          conversation.append(latest);
        }
        dates(conversation);
      } catch { return; }
    }
    positionAnchor();
  }
  document.addEventListener("DOMContentLoaded", async () => {
    await landAnchor();
    draft(false);
    const field = form()?.querySelector("textarea");
    if (field) grow(field);
    if (document.getElementById("conversation")?.dataset.activeRun || document.querySelector?.('[data-run-status="queued"], [data-run-status="running"]')) streamConversation();
  });
  globalThis.addEventListener?.("pageshow", (event) => {
    if (event.persisted) {
      document.querySelectorAll('form[aria-busy="true"]').forEach(target => {
        if (target === form()) return;
        target.removeAttribute("aria-busy");
        target.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = false; });
      });
      if (form()?.getAttribute("aria-busy") === "true") streamConversation();
    }
  });
  globalThis.addEventListener?.("pagehide", () => { draft(); events?.close(); events = null; clearInterval(timer); });
  document.addEventListener("keydown", (event) => {
    if (event.target.id === "message" && event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      const composer = form();
      if (composer && composer.getAttribute("aria-busy") !== "true" && !event.repeat) composer.requestSubmit();
    }
  });
  dates();
  jobSchedule();
  jobTools();
  if (!globalThis.location?.hash) scrollConversation();
})();
