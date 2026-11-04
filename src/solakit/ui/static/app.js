/* UI behavior only. Credentials never enter browser storage or application JS. */
(() => {
  let started = 0;
  let timer;
  const form = () => document.getElementById("composer");
  function dates(root = document) {
    root.querySelectorAll("time[datetime]").forEach((node) => {
      const date = new Date(node.dateTime);
      if (!Number.isNaN(date.valueOf())) {
        node.title = node.dateTime;
        node.textContent = new Intl.DateTimeFormat(undefined, {
          year: "numeric", month: "short", day: "numeric",
        }).format(date);
      }
    });
  }
  function scrollConversation() {
    const conversation = document.getElementById("conversation");
    if (conversation) conversation.scrollTop = conversation.scrollHeight;
  }
  function waiting(active) {
    const pending = document.getElementById("pending");
    const composer = form();
    if (!pending || !composer) return;
    clearInterval(timer);
    composer.setAttribute("aria-busy", String(active));
    pending.hidden = !active;
    if (active) {
      started = Date.now();
      const tick = () => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        document.getElementById("elapsed").textContent =
          `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      };
      tick();
      timer = setInterval(tick, 1000);
    }
  }
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt === form()) {
      document.getElementById("connection-error").hidden = true;
      waiting(true);
    }
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.elt === form()) waiting(false);
  });
  ["htmx:sendError", "htmx:timeout", "htmx:responseError"].forEach((name) => {
    document.addEventListener(name, (event) => {
      if (event.detail.elt === form()) {
        waiting(false);
        document.getElementById("connection-error").hidden = false;
      }
    });
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.target.id === "conversation" && [400, 404, 409, 413, 422, 503].includes(event.detail.xhr.status)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    dates(event.detail.target);
    if (event.detail.target.id === "conversation") scrollConversation();
  });
  document.addEventListener("chatCompleted", () => {
    const field = form()?.querySelector("textarea");
    if (field) { field.value = ""; field.focus(); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.id === "message" && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      const composer = form();
      if (composer && composer.getAttribute("aria-busy") !== "true") composer.requestSubmit();
    }
  });
  dates();
  scrollConversation();
})();
