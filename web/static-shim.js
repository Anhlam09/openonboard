// In-browser API for the static GitHub Pages demo.
// Starts Pyodide (Python in the browser), loads the app's own Python code, and answers the page's /api/...
// requests locally. No server is involved: each visitor's data stays in their own browser.
(function () {
  "use strict";
  const realFetch = window.fetch.bind(window);
  const isApi = (input) => typeof input === "string" && input.startsWith("/api/");

  // Loading notice, shown until Python is ready.
  const notice = document.createElement("div");
  notice.id = "static-loading";
  notice.setAttribute("role", "status");
  notice.setAttribute("aria-live", "polite");
  notice.innerHTML = '<div class="box"><p class="title">Starting the demo in your browser</p>' +
    '<p class="msg">This takes a few seconds the first time. Nothing you enter leaves your device.</p></div>';
  const style = document.createElement("style");
  style.textContent =
    "#static-loading{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;" +
    "background:rgba(0,0,0,.55);padding:1rem}" +
    "#static-loading .box{background:var(--surface,#fff);color:var(--text,#111);border:1px solid var(--border,#ccc);" +
    "border-radius:8px;padding:1.25rem 1.5rem;max-width:28rem;text-align:center}" +
    "#static-loading .title{font-weight:600;margin:0 0 .4rem}#static-loading .msg{margin:0}";
  document.head.appendChild(style);
  document.body.appendChild(notice);
  const setMessage = (text) => { notice.querySelector(".msg").textContent = text; };

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("Could not load " + src));
      document.head.appendChild(s);
    });
  }

  let py = null;
  let saving = Promise.resolve();
  function save() {  // copy the in-memory files to the browser's storage (IndexedDB)
    if (!py) return saving;
    saving = saving.then(() => new Promise((resolve) => py.FS.syncfs(false, () => resolve())));
    return saving;
  }

  async function boot() {
    const manifest = await (await realFetch("py/manifest.json")).json();
    const base = `https://cdn.jsdelivr.net/pyodide/v${manifest.pyodide_version}/full/`;
    await loadScript(base + "pyodide.js");
    py = await window.loadPyodide({ indexURL: base });
    setMessage("Loading Python packages…");
    await py.loadPackage(manifest.pyodide_packages);
    await py.pyimport("micropip").install(manifest.pip_packages);

    setMessage("Loading the app…");
    const home = "/home/pyodide/openonboard/";
    for (const f of manifest.files) {
      const text = await (await realFetch("py/" + f)).text();
      py.FS.mkdirTree((home + f).replace(/\/[^/]+$/, ""));
      py.FS.writeFile(home + f, text);
    }
    py.FS.mkdirTree("/data");
    try {  // keep the visitor's demo data between visits; falls back to memory only if storage is blocked
      py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
      await new Promise((resolve) => py.FS.syncfs(true, () => resolve()));
    } catch (e) { /* memory only */ }
    py.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(home)})`);
    return py.pyimport("bridge").handle;
  }

  const ready = boot().then((handle) => { notice.remove(); return handle; }).catch((err) => {
    setMessage("The demo couldn't start in this browser. Try reloading the page, or use a recent version of Chrome, Edge, Firefox or Safari. (" + err.message + ")");
    throw err;
  });

  async function callApi(path, opts = {}) {
    const handle = await ready;
    const req = new Request(new URL(path, location.href), opts);  // also encodes FormData uploads
    const body = ["GET", "HEAD"].includes(req.method) ? null : new Uint8Array(await req.arrayBuffer());
    const result = await handle(req.method, path, [...req.headers.entries()], body);
    const out = result.toJs({ dict_converter: Object.fromEntries });
    result.destroy();
    if (!["GET", "HEAD"].includes(req.method)) save();
    return new Response(out.status === 204 ? null : out.body, { status: out.status, headers: out.headers });
  }

  window.fetch = (input, opts) => (isApi(input) ? callApi(input, opts) : realFetch(input, opts));

  // Download links (exports, documents) can't reach a server, so fetch them here and save the file.
  document.addEventListener("click", async (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || !isApi(a.getAttribute("href"))) return;
    e.preventDefault();
    try {
      const r = await callApi(a.getAttribute("href"));
      if (!r.ok) throw new Error(`Request failed (${r.status})`);
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(r.headers.get("content-disposition") || "");
      const name = (match && decodeURIComponent(match[1])) || a.getAttribute("download") || "download";
      const url = URL.createObjectURL(await r.blob());
      const tmp = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.appendChild(tmp);
      tmp.click();
      tmp.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) {
      alert("The download didn't work: " + err.message);
    }
  }, true);
})();
