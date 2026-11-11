/* UI behavior only. Credentials never enter browser storage or application JS. */
(() => {
  let started = 0;
  let timer;
  let events;
  const form = () => document.getElementById("composer");
  const baseTitle = document.title;
  const menus = ".session-menu";
  // Popup disclosures close on outside clicks and Escape, like native menus.
  document.addEventListener("click", event => {
    document.querySelectorAll(menus).forEach(menu => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const menu = event.target.closest?.(menus) || [...document.querySelectorAll(menus)].find(node => node.open);
    if (!menu?.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
  // Copy controls are added only to settled content so streaming reconciliation never sees them.
  function copyButtons(root = document) {
    root.querySelectorAll(".markdown-content pre, .evidence-text, .tool-details pre").forEach(pre => {
      if (pre.parentElement.classList.contains("code-block")) return;
      if (pre.closest('[data-run-status="queued"], [data-run-status="running"], .live-preview')) return;
      pre.setAttribute("tabindex", "0");
      const wrap = document.createElement("div");
      wrap.className = "code-block";
      pre.replaceWith(wrap);
      wrap.append(pre);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "copy-button";
      button.textContent = "Copy";
      button.setAttribute("aria-label", "Copy text");
      wrap.append(button);
    });
  }
  document.addEventListener("click", async event => {
    const button = event.target.closest?.(".copy-button");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.parentElement.querySelector("pre").textContent);
      button.textContent = "Copied";
      button.dataset.copied = "";
      const status = document.getElementById("copy-status");
      if (status) status.textContent = "Text copied.";
    } catch {
      button.textContent = "Select to copy";
      const status = document.getElementById("copy-status");
      if (status) status.textContent = "Copy unavailable. Select the text to copy it.";
    }
    setTimeout(() => { button.textContent = "Copy"; delete button.dataset.copied; }, 1600);
  });
  function jumpVisibility() {
    const conversation = document.getElementById("conversation");
    const jump = document.getElementById("jump-latest");
    if (!conversation || !jump) return;
    jump.hidden = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 240;
  }
  document.addEventListener("scroll", event => {
    if (event.target.id === "conversation") jumpVisibility();
  }, true);
  document.addEventListener("click", event => {
    if (event.target.closest?.("#jump-latest")) { scrollConversation(); jumpVisibility(); }
  });
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
    jumpVisibility();
  }
  function waiting(active, createdAt, runId) {
    const pending = document.getElementById("pending");
    const composer = form();
    if (!pending || !composer) return;
    clearInterval(timer);
    composer.setAttribute("aria-busy", String(active));
    composer.querySelectorAll('button[type="submit"]').forEach((node) => { node.disabled = active; });
    pending.hidden = !active;
    const topic = document.querySelector?.('.conversation-link[aria-current="page"]');
    if (topic) {
      const spinner = topic.querySelector(".topic-running");
      if (spinner) spinner.hidden = !active;
      if (active) { const failed = topic.querySelector(".topic-failed"); if (failed) failed.hidden = true; }
    }
    document.title = (active ? "… " : "") + baseTitle;
    const stop = document.getElementById("stop-run");
    if (stop) {
      if (runId) stop.action = "/settings/evidence/runs/" + encodeURIComponent(runId) + "/cancel?return_to_session=true";
      if (!active) stop.removeAttribute("action");
      stop.querySelector('button[type="submit"]').disabled = !active || !stop.getAttribute("action");
    }
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
      copyButtons(conversation);
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
    const identity = runId || conversation.dataset.activeRun || pending?.dataset.runId;
    waiting(true, createdAt || conversation.dataset.activeCreatedAt || pending?.dataset.createdAt, identity);
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
      const current = [...conversation.querySelectorAll("[data-run-id]")].find(node => node.dataset.runId === snapshot.run_id && !node.dataset.archiveSegment);
      if (incoming) {
        if (current) reconcile(current, incoming);
        else { conversation.querySelector(".conversation-empty")?.remove(); conversation.append(incoming); }
        dates(current || incoming);
      }
      if (snapshot.pending) {
        conversation.dataset.activeRun = snapshot.run_id;
        conversation.dataset.activeCreatedAt = snapshot.created_at;
        waiting(true, snapshot.created_at, snapshot.run_id);
      }
      if (nearBottom) scrollConversation(); else jumpVisibility();
      if (!snapshot.pending) {
        const failed = document.querySelector?.('.conversation-link[aria-current="page"] .topic-failed');
        if (failed && incoming?.dataset.runStatus) failed.hidden = ["completed", "cancelled"].includes(incoming.dataset.runStatus);
        source.close();
        events = null;
        waiting(false);
        copyButtons(conversation);
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
      if (source === events) { draft(); source.close(); events = null; window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.search + window.location.hash)); }
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
      if (response.status === 401) { window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.search + window.location.hash)); return; }
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
        copyButtons(conversation);
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
  copyButtons();
  jobSchedule();
  jobTools();
  if (!globalThis.location?.hash) scrollConversation();
})();

// Navigation panels do not participate in transcript or streaming reconciliation.
(() => {
  let activePanel;
  let previousFocus;
  let detailsRequest;
  const inertBefore = new Map();
  const viewport = globalThis.matchMedia?.("(max-width: 900px)");
  const panel = id => document.getElementById(id);
  const mobile = () => Boolean(viewport?.matches);
  function restoreBackground() {
    inertBefore.forEach((wasInert, node) => { node.inert = wasInert; });
    inertBefore.clear();
    document.body?.classList.remove("panel-modal-open");
  }
  function syncModality() {
    restoreBackground();
    if (!activePanel) return;
    activePanel.removeAttribute("role");
    activePanel.removeAttribute("aria-modal");
    if (!mobile()) return;
    activePanel.setAttribute("role", "dialog");
    activePanel.setAttribute("aria-modal", "true");
    // Inert siblings at every ancestor level, never the dialog's ancestors.
    for (let node = activePanel; node?.parentElement && node !== document.body; node = node.parentElement) {
      [...node.parentElement.children].filter(sibling => sibling !== node).forEach(sibling => {
        inertBefore.set(sibling, sibling.inert);
        sibling.inert = true;
      });
    }
    document.body.classList.add("panel-modal-open");
    if (!activePanel.contains(document.activeElement)) activePanel.focus();
  }
  function close(id, returnFocus = true) {
    const node = panel(id);
    if (!node) return;
    if (id === "topic-details") detailsRequest?.abort();
    restoreBackground();
    if (id === "chat-sidebar") node.classList.remove("is-open");
    else node.hidden = true;
    document.querySelector(`[data-drawer-toggle="${id}"]`)?.setAttribute("aria-expanded", "false");
    node.removeAttribute("role");
    node.removeAttribute("aria-modal");
    activePanel = null;
    if (returnFocus && previousFocus?.isConnected && previousFocus.getClientRects().length) previousFocus.focus();
  }
  function localDates(root) {
    const formatter = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"});
    root.querySelectorAll("time[data-time]").forEach(node => {
      const date = new Date(node.dateTime);
      if (Number.isFinite(date.getTime())) node.textContent = formatter.format(date);
    });
  }
  async function refreshDetails(node, url = node.dataset.detailsUrl) {
    detailsRequest?.abort();
    const controller = new AbortController();
    detailsRequest = controller;
    node.setAttribute("aria-busy", "true");
    const status = node.querySelector("[data-details-status]");
    if (status) { status.hidden = false; status.textContent = "Refreshing details…"; }
    try {
      const response = await fetch(url, {signal: controller.signal});
      if (response.status === 401) { window.location.assign("/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash)); return; }
      if (!response.ok) throw new Error("Details unavailable");
      const template = document.createElement("template");
      template.innerHTML = await response.text(); // Authenticated, escaped UI fragment.
      if (controller.signal.aborted) return;
      const fresh = template.content.querySelector("#topic-details");
      if (!fresh) throw new Error("Details unavailable");
      // A delayed response must not strand focus on the body or steal it back
      // from the composer. Preserve the control the operator is using now.
      const focused = node.contains(document.activeElement) ? document.activeElement : null;
      const href = focused?.getAttribute("href");
      const closing = focused?.hasAttribute("data-drawer-close");
      node.dataset.detailsUrl = fresh.dataset.detailsUrl;
      node.replaceChildren(...fresh.childNodes);
      localDates(node);
      if (focused && focused !== node) {
        const replacement = closing ? node.querySelector("[data-drawer-close]") :
          [...node.querySelectorAll("a[href]")].find(link => link.getAttribute("href") === href);
        (replacement || node).focus();
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      const notice = node.querySelector("[data-details-status]");
      if (notice) { notice.hidden = false; notice.textContent = "Could not refresh Details. Reopen to retry."; }
    } finally {
      if (detailsRequest === controller) node.removeAttribute("aria-busy");
    }
  }
  function open(id, trigger) {
    const node = panel(id);
    if (!node) return;
    if (activePanel) close(activePanel.id, false);
    previousFocus = trigger;
    if (id === "chat-sidebar") node.classList.add("is-open");
    else node.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    activePanel = node;
    syncModality();
    node.focus();
    if (node.dataset.detailsUrl) refreshDetails(node);
  }
  viewport?.addEventListener("change", () => {
    if (activePanel?.id === "chat-sidebar" && !mobile()) {
      const focusInPanel = activePanel.contains(document.activeElement);
      close(activePanel.id, false);
      if (focusInPanel) panel("chat-sidebar").querySelector("a[aria-current], a")?.focus();
    } else syncModality();
  });
  globalThis.addEventListener?.("pagehide", () => { detailsRequest?.abort(); });
  document.addEventListener("click", event => {
    const trigger = event.target.closest?.("[data-drawer-toggle]");
    const closer = event.target.closest?.("[data-drawer-close]");
    const page = event.target.closest?.("[data-details-page]");
    if (page && activePanel?.id === "topic-details" && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
      event.preventDefault();
      refreshDetails(activePanel, page.dataset.detailsPage);
    } else if (trigger) {
      if (trigger.getAttribute("aria-expanded") === "true") close(trigger.dataset.drawerToggle);
      else open(trigger.dataset.drawerToggle, trigger);
    } else if (closer) close(closer.dataset.drawerClose);
    else if (activePanel && mobile() && !activePanel.contains(event.target)) close(activePanel.id);
  });
  document.addEventListener("focusin", event => {
    if (activePanel && mobile() && !activePanel.contains(event.target)) activePanel.focus();
  });
  document.addEventListener("keydown", event => {
    if (!activePanel) return;
    if (event.key === "Escape") { event.preventDefault(); close(activePanel.id); return; }
    if (event.key !== "Tab" || !mobile()) return;
    const stops = [...activePanel.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex="0"]')].filter(el => el.getClientRects().length && !el.disabled && !el.closest("[inert]"));
    const first = stops[0], last = stops[stops.length - 1];
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === activePanel)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || document.activeElement === activePanel)) { event.preventDefault(); first.focus(); }
  });
  document.addEventListener("input", event => {
    if (event.target.id !== "topic-filter") return;
    const query = event.target.value.trim().toLocaleLowerCase();
    let matching = 0;
    document.querySelectorAll("[data-topic-title]").forEach(link => {
      link.hidden = !link.dataset.topicTitle.includes(query);
      if (!link.hidden) matching++;
    });
    const archived = document.querySelector(".archived-topics");
    if (archived) {
      if (query) {
        if (!("filterWasOpen" in archived.dataset)) archived.dataset.filterWasOpen = String(archived.open);
        archived.open = [...archived.querySelectorAll("[data-topic-title]")].some(link => !link.hidden);
      } else if ("filterWasOpen" in archived.dataset) {
        archived.open = archived.dataset.filterWasOpen === "true";
        delete archived.dataset.filterWasOpen;
      }
    }
    const status = panel("topic-filter-status");
    if (status) status.textContent = query ? (matching ? `${matching} matching ${matching === 1 ? "topic" : "topics"} on this page` : "No matching topics on this page. Try another title.") : "";
  });
  document.addEventListener("DOMContentLoaded", () => {
    const dot = panel("health-indicator");
    if (dot) {
      let stopped = false;
      let polling;
      async function update() {
        try {
          const response = await fetch("/ui/health", {headers: {Accept: "application/json"}});
          if (response.status === 401) { stopped = true; return; }
          if (!response.ok) throw new Error("Health unavailable");
          const state = await response.json();
          dot.className = "health-indicator health-" + state.color;
          dot.title = state.label;
          dot.setAttribute("aria-label", "System: " + state.label);
        } catch {
          dot.className = "health-indicator health-amber";
          dot.title = "System health unavailable";
          dot.setAttribute("aria-label", dot.title);
        } finally { if (!stopped) polling = setTimeout(update, 15000); }
      }
      update();
      globalThis.addEventListener?.("pagehide", () => { stopped = true; clearTimeout(polling); });
      globalThis.addEventListener?.("pageshow", event => { if (event.persisted) { stopped = false; update(); } });
    }
    const relative = new Intl.RelativeTimeFormat(undefined, {numeric: "auto"});
    const absolute = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"});
    document.querySelectorAll("[data-relative-time]").forEach(node => {
      const date = new Date(node.dateTime);
      if (!Number.isFinite(date.getTime())) return;
      const seconds = Math.min(0, (date.getTime() - Date.now()) / 1000);
      const [value, unit] = Math.abs(seconds) >= 86400 ? [seconds / 86400, "day"] : Math.abs(seconds) >= 3600 ? [seconds / 3600, "hour"] : [seconds / 60, "minute"];
      node.textContent = relative.format(Math.ceil(value), unit);
      node.title = absolute.format(date);
    });
  });
})();
