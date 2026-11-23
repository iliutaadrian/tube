// Tube web app. Vanilla JS, no build step. Hash routes, state lives on the server.
(() => {
  "use strict";
  const view = document.getElementById("view");
  const searchBox = document.getElementById("q");
  const TOKEN_KEY = "tube.token";
  let player = null;       // active YouTube player, if any
  let ticker = null;       // progress reporting interval
  let renderId = 0;        // guards against a slow page landing after a newer one

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const token = () => { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } };

  async function api(path, opts = {}) {
    const res = await fetch("/api/" + path, {
      method: opts.method || "GET",
      headers: { Authorization: "Bearer " + token(), ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) { askToken(); throw new Error("Token required"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ")");
    return data;
  }

  function askToken() {
    stopPlayer();
    view.innerHTML = `<h1>Token</h1><p class="sub">Paste the app token from tube.json. It is stored only in this browser.</p>
      <form class="inline" id="tokenForm"><input type="password" id="tokenInput" placeholder="App token" autocomplete="off" aria-label="App token">
      <button class="btn">Save</button></form>`;
    document.getElementById("tokenForm").addEventListener("submit", (e) => {
      e.preventDefault();
      try { localStorage.setItem(TOKEN_KEY, document.getElementById("tokenInput").value.trim()); } catch { /* private mode */ }
      route();
    });
  }

  function clock(sec) {
    if (!sec) return "";
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }

  function ago(ts) {
    if (!ts) return "";
    const d = (Date.now() / 1000 - ts) / 86400;
    if (d < 1 / 24) { const m = Math.round(d * 1440); return m < 1 ? "just now" : m + (m === 1 ? " minute ago" : " minutes ago"); }
    if (d < 1) { const h = Math.round(d * 24); return h + (h === 1 ? " hour ago" : " hours ago"); }
    const steps = [[1, "day"], [7, "week"], [30.4, "month"], [365, "year"]];
    let out = "";
    for (const [size, name] of steps) if (d >= size) { const n = Math.floor(d / size); out = n + " " + name + (n === 1 ? "" : "s") + " ago"; }
    return out;
  }

  const count = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : String(n);

  function cardHtml(v) {
    const length = v.duration || v.seen_duration;
    const pct = !v.watched && v.progress && length ? Math.min(100, (v.progress / length) * 100) : 0;
    const meta = v.source === "sub"
      ? `<a href="#/subs/${esc(v.channel_id)}">${esc(v.channel)}</a>${v.published ? " · " + ago(v.published) : ""}`
      : `${esc(v.channel)}${v.views != null ? " · " + count(v.views) + " views" : ""}`;
    return `<article class="card${v.watched ? " watched" : ""}" data-vid="${esc(v.vid)}">
      <a class="thumb" href="#/watch/${esc(v.vid)}" aria-label="${esc(v.title)}">
        <img loading="lazy" alt="" src="https://i.ytimg.com/vi/${esc(v.vid)}/mqdefault.jpg">
        ${v.watched ? '<span class="badge seen">Watched</span>' : ""}
        ${v.duration ? `<span class="badge">${clock(v.duration)}</span>` : ""}
        ${pct ? `<span class="bar" style="width:${pct.toFixed(0)}%"></span>` : ""}
      </a>
      <button class="save${v.later ? " on" : ""}" data-later="${v.later ? 1 : 0}" title="Watch later" aria-label="Watch later" aria-pressed="${!!v.later}">${v.later ? "✓" : "+"}</button>
      <div><h3><a href="#/watch/${esc(v.vid)}">${esc(v.title)}</a></h3>
        <div class="meta">${meta}</div></div>
    </article>`;
  }
  const gridHtml = (videos) => `<div class="grid">${videos.map(cardHtml).join("")}</div>`;
  // A grid that grows only when asked: one "Show more" button, no infinite scroll.
  async function pagedGrid(holder, load, emptyText) {
    const mine = renderId;
    holder.innerHTML = `<div class="grid"></div>`;
    const grid = holder.firstElementChild;
    async function next(offset) {
      const data = await load(offset);
      if (mine !== renderId) return data;
      holder.querySelector(".more")?.remove();
      if (!data.videos.length && !offset) { holder.innerHTML = `<p class="empty">${esc(emptyText)}</p>`; return data; }
      grid.insertAdjacentHTML("beforeend", data.videos.map(cardHtml).join(""));
      if (data.next != null) {
        const btn = document.createElement("button");
        btn.className = "btn more";
        btn.textContent = "Show more";
        btn.addEventListener("click", () => { btn.disabled = true; next(data.next).catch(fail); });
        holder.appendChild(btn);
      }
      return data;
    }
    return next(0);
  }

  function fail(err) {
    if (err.message === "Token required") return;
    view.insertAdjacentHTML("afterbegin", `<p class="error" role="alert">${esc(err.message)}</p>`);
  }

  // ---------- pages ----------
  async function home() {
    const { shelves } = await api("home");
    if (!shelves.length) return `<p class="empty">Nothing here yet. Add channels and <a href="#/interests">interests</a>, then try again in a minute.</p>`;
    return shelves.map((s) => `<section class="shelf"><div class="shelf-head"><h2>${esc(s.title)}</h2>
      ${s.link ? `<a href="${esc(s.link)}">See all</a>` : ""}</div>${gridHtml(s.videos)}</section>`).join("");
  }

  async function subs(channel) {
    const { channels } = await api("channels");
    view.innerHTML = `<h1>Subscriptions</h1><p class="sub">Newest first. No Shorts, nothing you did not subscribe to.</p>
      <div class="chips"><a class="chip${channel ? "" : " on"}" href="#/subs">All</a>${channels.map((c) =>
        `<a class="chip${c.id === channel ? " on" : ""}" href="#/subs/${esc(c.id)}">${esc(c.name)}</a>`).join("")}</div><div id="list"></div>`;
    await pagedGrid(document.getElementById("list"),
      (offset) => api("subs?offset=" + offset + (channel ? "&channel=" + encodeURIComponent(channel) : "")),
      channels.length ? "No videos yet. The first load of a channel can take a minute." : "No channels yet. Add some under Channels.");
  }

  async function forYou(interest) {
    const { interests } = await api("interests");
    view.innerHTML = `<h1>For you</h1><p class="sub">YouTube's search results for your interests, taken in turns. Watched videos drop out.</p>
      <div class="chips"><a class="chip${interest ? "" : " on"}" href="#/foryou">All</a>${interests.map((i) =>
        `<a class="chip${String(i.id) === interest ? " on" : ""}" href="#/foryou/${i.id}">${esc(i.name)}</a>`).join("")}
        <a class="chip" href="#/interests">Edit interests</a></div><div id="list"></div>`;
    await pagedGrid(document.getElementById("list"),
      (offset) => api("foryou?offset=" + offset + (interest ? "&interest=" + encodeURIComponent(interest) : "")),
      interests.length ? "Nothing new. Results are refreshed once a day." : "No interests yet. Add some under Interests.");
  }

  async function interestsPage() {
    const { interests } = await api("interests");
    view.innerHTML = `<h1>Interests</h1><p class="sub">What you are into right now. Each one is searched on YouTube once a day and feeds “For you”.</p>
      <form class="inline" id="addForm"><input id="addInput" placeholder="e.g. woodworking jigs" maxlength="60" autocomplete="off" aria-label="Interest to add">
      <button class="btn" id="addBtn">Add</button></form>
      <div class="box rows">${interests.map((i) => `<div class="row"><div class="grow"><a href="#/foryou/${i.id}"><strong>${esc(i.name)}</strong></a>
        <div class="meta">${i.videos} videos${i.refreshed_at ? " · searched " + ago(i.refreshed_at) : " · first search pending"}${
          i.error ? ` · <span class="error">${esc(i.error)}</span>` : ""}</div></div>
        <button class="btn danger" data-remove="${i.id}" data-name="${esc(i.name)}">Remove</button></div>`).join("") || '<p class="empty">No interests yet.</p>'}</div>`;
    document.getElementById("addForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("addBtn");
      btn.disabled = true; btn.textContent = "Searching YouTube…";
      try { await api("interests", { method: "POST", body: { name: document.getElementById("addInput").value } }); route(); }
      catch (err) { btn.disabled = false; btn.textContent = "Add"; fail(err); }
    });
    view.querySelectorAll("[data-remove]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm("Remove the interest “" + btn.dataset.name + "”?")) return;
      try { await api("interests/" + encodeURIComponent(btn.dataset.remove), { method: "DELETE" }); route(); } catch (err) { fail(err); }
    }));
  }

  async function list(kind, title, empty) {
    view.innerHTML = `<h1>${esc(title)}</h1><div id="list"></div>`;
    await pagedGrid(document.getElementById("list"), (offset) => api("list?kind=" + kind + "&offset=" + offset), empty);
  }

  async function search(params) {
    const q = params.get("q") || "";
    searchBox.value = q;
    const data = await api("search?q=" + encodeURIComponent(q));
    const part = (title, videos) => videos.length ? `<section class="shelf"><div class="shelf-head"><h2>${title}</h2></div>${gridHtml(videos)}</section>` : "";
    return `<h1>Search</h1><p class="sub">Results for “${esc(q)}”</p>${data.error ? `<p class="error" role="alert">YouTube search failed: ${esc(data.error)}</p>` : ""}` +
      (part("Your channels", data.subs) + part("YouTube", data.youtube) || `<p class="empty">No matches.</p>`);
  }

  async function channelsPage() {
    const { channels } = await api("channels");
    view.innerHTML = `<h1>Channels</h1><p class="sub">Your subscriptions. New uploads are checked every hour.</p>
      <form class="inline" id="addForm"><input id="addInput" placeholder="Channel URL, @handle or UC… id" autocomplete="off" aria-label="Channel to add">
      <button class="btn" id="addBtn">Subscribe</button></form>
      <div class="box rows">${channels.map((c) => `<div class="row"><div class="grow"><a href="#/subs/${esc(c.id)}"><strong>${esc(c.name)}</strong></a>
        <div class="meta">${c.videos} videos${c.refreshed_at ? " · checked " + ago(c.refreshed_at) : " · first load pending"}${
          c.error ? ` · <span class="error">${esc(c.error)}</span>` : ""}</div></div>
        <button class="btn danger" data-remove="${esc(c.id)}" data-name="${esc(c.name)}">Remove</button></div>`).join("") || '<p class="empty">No channels yet.</p>'}</div>`;
    document.getElementById("addForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("addBtn");
      btn.disabled = true; btn.textContent = "Reading channel…";
      try { await api("channels", { method: "POST", body: { url: document.getElementById("addInput").value } }); route(); }
      catch (err) { btn.disabled = false; btn.textContent = "Subscribe"; fail(err); }
    });
    view.querySelectorAll("[data-remove]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm("Unsubscribe from " + btn.dataset.name + "?")) return;
      try { await api("channels/" + encodeURIComponent(btn.dataset.remove), { method: "DELETE" }); route(); } catch (err) { fail(err); }
    }));
  }

  async function watch(vid) {
    const mine = renderId;
    const data = await api("video/" + encodeURIComponent(vid));
    if (mine !== renderId) return;
    const v = data.video;
    const start = !v.watched && v.progress > 30 ? Math.floor(v.progress) : 0;
    const src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(vid) + "?" + new URLSearchParams({
      enablejsapi: 1, rel: 0, playsinline: 1, origin: location.origin, ...(start ? { start } : {}) });
    const side = [...data.more, ...data.related];
    view.innerHTML = `<div class="watch"><div>
      <div class="player"><iframe id="frame" src="${esc(src)}" title="${esc(v.title)}" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
      <h1>${esc(v.title)}</h1>
      <div class="meta">${v.channel_id ? `<a href="#/subs/${esc(v.channel_id)}">${esc(v.channel)}</a>` : esc(v.channel)}${
        v.published ? " · " + ago(v.published) : ""}${v.duration ? " · " + clock(v.duration) : ""}</div>
      <div class="actions">
        <button class="btn${v.liked ? " on" : ""}" data-toggle="liked" aria-pressed="${!!v.liked}">${v.liked ? "Liked" : "Like"}</button>
        <button class="btn${v.later ? " on" : ""}" data-toggle="later" aria-pressed="${!!v.later}">${v.later ? "Saved for later" : "Watch later"}</button>
        <button class="btn${v.watched ? " on" : ""}" data-toggle="watched" aria-pressed="${!!v.watched}">${v.watched ? "Watched" : "Mark watched"}</button>
        <button class="btn" data-toggle="hidden" aria-pressed="false" title="Remove from For you">Not interested</button>
        ${data.download.enabled ? '<button class="btn" id="dl"></button>' : ""}
        <a class="btn" href="https://www.youtube.com/watch?v=${esc(vid)}" target="_blank" rel="noopener">Open on YouTube</a>
      </div><p class="error" id="dlError" role="status"></p>
      <div class="box" id="desc"><div class="label">Description</div><p class="meta">Loading…</p></div>
      </div>
      <aside class="side" aria-label="${data.more.length ? "More from this channel" : "Related"}">${side.map(cardHtml).join("")}</aside></div>`;

    view.querySelectorAll("[data-toggle]").forEach((btn) => btn.addEventListener("click", async () => {
      const key = btn.dataset.toggle, on = btn.getAttribute("aria-pressed") !== "true";
      try {
        await api("state", { method: "POST", body: { vid, [key]: on } });
        btn.setAttribute("aria-pressed", on);
        btn.classList.toggle("on", on);
        btn.textContent = { liked: ["Like", "Liked"], later: ["Watch later", "Saved for later"], watched: ["Mark watched", "Watched"], hidden: ["Not interested", "Hidden from For you"] }[key][on ? 1 : 0];
      } catch (err) { fail(err); }
    }));
    downloadButton(vid, data.download, mine);
    loadDescription(vid, mine);
    trackPlayback(vid);
  }

  // The uploader's own description. Untrusted text: escaped first, then only http(s) links are made clickable.
  function linkify(text) {
    return esc(text).replace(/https?:\/\/[^\s<]+[^\s<.,;:!?)\]]/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  }

  async function loadDescription(vid, mine) {
    let d, error;
    try { d = await api("description/" + encodeURIComponent(vid)); } catch (err) { error = err.message; }
    const box = document.getElementById("desc");
    if (mine !== renderId || !box) return;
    if (error || !d.text.trim()) { box.innerHTML = `<div class="label">Description</div><p class="meta">${esc(error || "No description.")}</p>`; return; }
    const date = /^\d{8}$/.test(d.upload_date || "") ? new Date(d.upload_date.replace(/(\d{4})(\d\d)(\d\d)/, "$1-$2-$3")).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
    const facts = [date && "Uploaded " + date, d.views != null && d.views.toLocaleString() + " views"].filter(Boolean).join(" · ");
    const long = d.text.length > 400 || d.text.split("\n").length > 6;
    box.innerHTML = `<div class="label">Description</div>${facts ? `<p class="meta">${esc(facts)}</p>` : ""}
      <div class="desc-text${long ? " clamp" : ""}">${linkify(d.text)}</div>
      ${long ? '<button class="chip" id="descMore" aria-expanded="false">Show more</button>' : ""}`;
    document.getElementById("descMore")?.addEventListener("click", (e) => {
      const open = box.querySelector(".desc-text").classList.toggle("clamp") === false;
      e.target.textContent = open ? "Show less" : "Show more";
      e.target.setAttribute("aria-expanded", open);
    });
  }

  // One push sends the video to the homelab's Plex folder. The button then follows the job.
  function downloadButton(vid, first, mine) {
    const btn = document.getElementById("dl"), note = document.getElementById("dlError");
    if (!btn) return;
    const show = (d) => {
      if (mine !== renderId) return;
      const busy = d.status === "queued" || d.status === "running";
      btn.textContent = { queued: "Queued for Plex…", running: "Downloading to Plex…", done: "On Plex", failed: "Download failed, retry" }[d.status] || "Download to Plex";
      btn.disabled = busy || d.status === "done";
      btn.classList.toggle("on", d.status === "done");
      note.textContent = d.status === "failed" ? d.error || "" : "";
      if (busy) setTimeout(() => { if (mine === renderId) api("download/" + encodeURIComponent(vid)).then(show).catch(() => show(d)); }, 5000);
    };
    btn.addEventListener("click", () => {
      btn.disabled = true;
      api("download", { method: "POST", body: { vid } }).then(show).catch((err) => { btn.disabled = false; fail(err); });
    });
    show(first);
  }

  // ---------- playback tracking ----------
  function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    return new Promise((resolve) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { prev && prev(); resolve(); };
      if (!document.getElementById("ytapi")) {
        const s = document.createElement("script");
        s.id = "ytapi"; s.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(s);
      }
    });
  }

  function stopPlayer() {
    clearInterval(ticker); ticker = null;
    if (player) { try { player.destroy(); } catch { /* frame already gone */ } player = null; }
  }

  async function trackPlayback(vid) {
    const mine = renderId;
    await loadYouTubeApi();
    if (mine !== renderId || !document.getElementById("frame")) return;
    let done = false;
    const report = (ended) => {
      if (!player || typeof player.getCurrentTime !== "function") return;
      const at = player.getCurrentTime() || 0, total = player.getDuration() || 0;
      if (!total || at < 5) return;
      const body = { vid, progress: at, duration: total };
      if (!done && (ended || at / total >= 0.9)) { body.watched = true; body.later = false; done = true; }
      api("state", { method: "POST", body }).catch(() => {});
    };
    player = new YT.Player("frame", { events: { onStateChange: (e) => {
      clearInterval(ticker); ticker = null;
      if (e.data === YT.PlayerState.PLAYING) ticker = setInterval(() => report(false), 15000);
      if (e.data === YT.PlayerState.PAUSED) report(false);
      if (e.data === YT.PlayerState.ENDED) report(true);
    } } });
  }

  // ---------- routing ----------
  async function route() {
    const mine = ++renderId;
    stopPlayer();
    const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const params = new URLSearchParams(query);
    const section = parts[0] || "home";
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === section));
    if (section !== "search") searchBox.value = "";
    view.innerHTML = `<p class="empty">Loading…</p>`;
    window.scrollTo(0, 0);
    try {
      let html;
      if (section === "home") html = await home();
      else if (section === "subs") await subs(parts[1]);
      else if (section === "foryou") await forYou(parts[1]);
      else if (section === "interests") await interestsPage();
      else if (section === "later") await list("later", "Watch later", "Nothing saved. Use the + on any video.");
      else if (section === "liked") await list("liked", "Liked", "No likes yet.");
      else if (section === "history") await list("history", "History", "Nothing watched yet.");
      else if (section === "channels") await channelsPage();
      else if (section === "search") html = await search(params);
      else if (section === "watch" && parts[1]) await watch(parts[1]);
      else html = `<p class="empty">Page not found.</p>`;
      if (html !== undefined && mine === renderId) view.innerHTML = html;
    } catch (err) {
      if (mine === renderId && err.message !== "Token required") {
        const offline = err instanceof TypeError;  // fetch could not reach the server
        view.innerHTML = `<p class="error" role="alert">${esc(offline ? "Cannot reach Tube. It only answers on the home network." : err.message)}</p>
          <p class="empty"><button class="btn" id="retry">Try again</button></p>`;
        document.getElementById("retry").addEventListener("click", route);
      }
    }
  }

  // Watch later buttons on cards, anywhere in the page.
  view.addEventListener("click", async (e) => {
    const btn = e.target.closest(".save");
    if (!btn) return;
    const on = btn.dataset.later !== "1";
    try {
      await api("state", { method: "POST", body: { vid: btn.closest(".card").dataset.vid, later: on } });
      btn.dataset.later = on ? 1 : 0;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on);
      btn.textContent = on ? "✓" : "+";
    } catch (err) { fail(err); }
  });

  document.getElementById("search").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = searchBox.value.trim();
    if (q) location.hash = "#/search?q=" + encodeURIComponent(q);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) { e.preventDefault(); searchBox.focus(); }
  });
  window.addEventListener("hashchange", route);
  route();
  // Installed-app support. Service workers need HTTPS (or localhost); on plain HTTP this is skipped.
  if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("sw.js").catch(() => {});
})();
