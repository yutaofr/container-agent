// The run page's client: keeps the regions current from the run's event stream.
//
// The server renders every fragment; this module only decides when to fetch one and whether
// a response is still current. It never builds HTML from data. Design: M8b section 5.
(() => {
  "use strict";
  const root = document.getElementById("run");
  if (!root) return;
  const runId = root.dataset.runId;
  const BUSINESS = [
    "run_state", "observation", "observation_failed", "runner",
    "phase_started", "phase_completed", "phase_interrupted", "phase_failed",
  ];
  const AFTER = {
    run_state: ["state", "record", "commands"],
    observation: ["state", "record"],
    observation_failed: ["state", "record"],
    runner: ["state", "record"],
  };

  const s = {
    generation: 0,                         // bumped by resync and expiry: older work is void
    lastEventSeq: Number(root.dataset.eventSeq),
    recordAfter: Number(root.dataset.recordAfter),
    seen: new Set(),                       // record entries already on the page, by seq
    dirty: new Set(),
    inflight: new Set(),
    timer: null,
    backoff: 250,
    expired: false,
    es: null,
  };

  class Void extends Error {}              // a response from an earlier generation
  class Expired extends Error {}

  async function get(url) {
    const generation = s.generation;
    const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (r.status === 401) { expire(); throw new Expired(); }
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    const text = await r.text();
    if (generation !== s.generation || s.expired) throw new Void();
    return { r, text };
  }

  function template(text) {
    // Fragments come only from this origin's server, rendered by Jinja with autoescaping;
    // parsing them into an inert <template> runs nothing (and the CSP forbids inline script).
    const t = document.createElement("template");
    t.innerHTML = text;
    return t.content.querySelector("template");
  }

  // --- refresh queue --------------------------------------------------------------------

  function mark(...names) {
    if (s.expired) return;
    names.forEach((n) => s.dirty.add(n));
    schedule(100);
  }

  function schedule(delay) {
    if (s.timer || s.expired) return;
    s.timer = setTimeout(flush, delay);
  }

  function flush() {
    s.timer = null;
    for (const name of [...s.dirty]) {
      if (s.inflight.has(name)) continue;  // fetched again when the current one ends
      s.dirty.delete(name);
      s.inflight.add(name);
      JOBS[name]()
        .then(() => { s.backoff = 250; })
        .catch((e) => {
          if (e instanceof Void || e instanceof Expired) return;
          s.dirty.add(name);               // stays dirty; retried with backoff
          s.backoff = Math.min(s.backoff * 2, 10000);
        })
        .finally(() => {
          s.inflight.delete(name);
          if (s.dirty.size) schedule(s.backoff);
        });
    }
  }

  // --- regions --------------------------------------------------------------------------

  async function refreshState() {
    const { r, text } = await get(`/runs/${runId}/fragments/state`);
    applyState(template(text));
    return Number(r.headers.get("Cagent-Event-Seq"));
  }

  function applyState(tpl) {
    for (const fresh of tpl.content.querySelectorAll("section[data-region]")) {
      const name = fresh.dataset.region;
      const current = document.getElementById(name);
      if (!current || Number(fresh.dataset.seq) < Number(current.dataset.seq)) continue;
      if (name === "pending") applyPending(current, fresh);
      else htmx.swap(current, fresh.outerHTML, { swapStyle: "outerHTML" });
    }
  }

  // A card being edited stays while it is the same item at the same revision. Otherwise
  // the old card is disabled, keeps its input, and the new one comes in beside it.
  function applyPending(current, fresh) {
    const same = current.dataset.pendingId !== "" &&
      current.dataset.pendingId === fresh.dataset.pendingId &&
      current.dataset.revision === fresh.dataset.revision;
    const busy = current.querySelector("form[data-modified], form[data-sending]");
    if (busy && same) { current.dataset.seq = fresh.dataset.seq; return; }
    const old = busy ? busy.closest("article") : null;
    htmx.swap(current, fresh.outerHTML, { swapStyle: "outerHTML" });
    if (old) {
      retire(old, "This item changed since you started. Your input is kept here for reference.");
      document.getElementById("pending").prepend(old);
    }
  }

  function retire(article, message) {
    article.dataset.stale = "true";
    article.querySelectorAll("button, input, select, textarea").forEach((el) => { el.disabled = true; });
    const note = document.createElement("p");
    note.className = "error stale";
    note.textContent = message;
    article.prepend(note);
  }

  async function syncRecord() {
    for (;;) {
      const { r, text } = await get(`/runs/${runId}/fragments/record?after=${s.recordAfter}`);
      const list = root.querySelector("ol.record");
      let open = false;
      for (const li of template(text).content.querySelectorAll("li")) {
        const seq = li.dataset.seq;
        if (seq && s.seen.has(seq)) continue;
        if (seq) s.seen.add(seq);
        list.append(li);
        if (li.querySelector('[data-status="accepted"]')) open = true;
      }
      s.recordAfter = Number(r.headers.get("Cagent-Record-After"));  // after appending
      if (open) mark("commands");
      if (r.headers.get("Cagent-Record-More") !== "true") return;
    }
  }

  async function refreshCommands() {
    const open = root.querySelectorAll('.command[data-status="accepted"]');
    for (const span of open) {
      const { text } = await get(`/runs/${runId}/fragments/command/${span.dataset.commandId}`);
      htmx.swap(span, text, { swapStyle: "outerHTML" });
    }
    if (root.querySelector('.command[data-status="accepted"]')) mark("commands");
  }

  const JOBS = { state: refreshState, record: syncRecord, commands: refreshCommands };

  // --- the event stream -----------------------------------------------------------------

  function connect() {
    const es = new EventSource(`/api/runs/${runId}/events?after=${s.lastEventSeq}`);
    // Control events first: they have no id and may carry the last one over.
    es.addEventListener("resync", () => resync());
    es.addEventListener("session_expired", () => expire());
    for (const type of BUSINESS) {
      es.addEventListener(type, (ev) => {
        const seq = Number(ev.lastEventId);
        if (!(seq > s.lastEventSeq)) return;
        s.lastEventSeq = seq;
        mark(...(AFTER[type] || ["state", "record"]));
      });
    }
    es.onerror = () => {
      // The browser reconnects by itself after a network error; a refused reconnect ends
      // the stream for good, and a state fetch tells why (401 means the session ended).
      if (es.readyState === EventSource.CLOSED && !s.expired) resync();
    };
    s.es = es;
  }

  async function resync() {
    if (s.expired) return;
    if (s.es) s.es.close();
    s.generation += 1;
    s.dirty.clear();
    try {
      s.lastEventSeq = await refreshState();
      await syncRecord();
      await refreshCommands();
      connect();
    } catch (e) {
      if (e instanceof Expired) return;
      setTimeout(resync, (s.backoff = Math.min(s.backoff * 2, 10000)));
    }
  }

  function expire() {
    if (s.expired) return;
    s.expired = true;
    s.generation += 1;
    if (s.es) s.es.close();
    clearTimeout(s.timer);
    document.querySelectorAll("form button").forEach((b) => { b.disabled = true; });
    const banner = document.createElement("div");
    banner.className = "banner";
    banner.setAttribute("role", "alert");
    banner.append("Your session ended. Your input is kept on this page. ");
    const link = document.createElement("a");
    link.href = "/login";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Log in again";
    banner.append(link, ", then reload this page. A request you sent may still have run: its result shows after reloading.");
    document.body.prepend(banner);
  }

  // --- submitting cards -----------------------------------------------------------------

  document.addEventListener("input", (ev) => {
    const form = ev.target.closest && ev.target.closest("#pending form");
    if (form) form.dataset.modified = "true";
  });

  document.body.addEventListener("htmx:configRequest", (ev) => {
    const form = ev.detail.elt.closest("form");
    if (!form || !root.contains(form)) return;
    if (form.frozen) {                     // a retry sends the first submission, unchanged
      for (const key of [...ev.detail.formData.keys()]) ev.detail.formData.delete(key);
      for (const [k, v] of form.frozen) ev.detail.formData.append(k, v);
    } else {
      form.frozen = [...ev.detail.formData.entries()];
    }
    form.dataset.sending = "true";
    form.inert = true;                     // no edits while the result is unknown
  });

  document.body.addEventListener("htmx:beforeSwap", (ev) => {
    const article = ev.detail.target && ev.detail.target.closest && ev.detail.target.closest("article");
    if (s.expired || ev.detail.xhr.status === 401) {
      ev.detail.shouldSwap = false;
      expire();
      return;
    }
    // A late "accepted" never replaces a newer card; the record shows its command's result.
    // Errors (409, 422) go to the card's own error area and are always shown.
    if (ev.detail.xhr.status >= 300) return;
    const region = document.getElementById("pending");
    const card = ev.detail.requestConfig && ev.detail.requestConfig.elt.closest("article");
    if (card && (!region.contains(card) || card.dataset.stale)) ev.detail.shouldSwap = false;
    if (article && article.dataset.stale) ev.detail.shouldSwap = false;
  });

  document.body.addEventListener("htmx:afterRequest", (ev) => {
    const form = ev.detail.elt.closest && ev.detail.elt.closest("form");
    const status = ev.detail.xhr ? ev.detail.xhr.status : 0;
    if (form && status === 422) {          // refused, nothing consumed: input can change
      form.frozen = null;
      delete form.dataset.sending;
      form.inert = false;
      form.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
    if (status === 202 || status === 409) mark("state", "record", "commands");
  });

  document.body.addEventListener("cagent:stale", (ev) => {
    const article = ev.target.closest && ev.target.closest("article");
    if (article) retire(article, "This item is out of date.");
  });

  function unknown(ev) {
    const form = ev.detail.elt.closest && ev.detail.elt.closest("form");
    if (!form) return;
    const area = form.querySelector(".card-error");
    area.textContent = "";
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = "The result is unknown: the request may or may not have arrived. ";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Retry the same request";
    retry.addEventListener("click", () => {
      form.inert = false;
      htmx.trigger(form, "submit");        // configRequest restores the frozen submission
    });
    const check = document.createElement("button");
    check.type = "button";
    check.textContent = "Check this command";
    check.addEventListener("click", async () => {
      const id = new Map(form.frozen || []).get("command_id");
      const r = await fetch(`/runs/${runId}/fragments/command/${id}`, { credentials: "same-origin" });
      p.textContent = r.ok ? "" : "This command did not arrive. Retry it.";
      if (r.ok) htmx.swap(p, await r.text(), { swapStyle: "innerHTML" });
    });
    area.append(p, retry, check);
  }
  document.body.addEventListener("htmx:sendError", unknown);
  document.body.addEventListener("htmx:timeout", unknown);

  // --- start ----------------------------------------------------------------------------

  mark("record", "commands");
  connect();
})();
