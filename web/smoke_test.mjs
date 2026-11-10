// Smoke test for the static demo: runs the built _site/py code in Pyodide under Node and walks a full application.
//   python scripts/build_static.py && npm install --no-save pyodide@<version> && node web/smoke_test.mjs
// Set PYODIDE_MODULE to the path of pyodide.mjs if it isn't installed next to this repo.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const site = join(dirname(fileURLToPath(import.meta.url)), "..", "_site", "py");
const manifest = JSON.parse(readFileSync(join(site, "manifest.json"), "utf8"));
const { loadPyodide } = await import(process.env.PYODIDE_MODULE || "pyodide");

const py = await loadPyodide();
await py.loadPackage(manifest.pyodide_packages);
await py.pyimport("micropip").install(manifest.pip_packages);
const home = "/home/pyodide/openonboard/";
for (const f of manifest.files) {
  py.FS.mkdirTree((home + f).replace(/\/[^/]+$/, ""));
  py.FS.writeFile(home + f, readFileSync(join(site, f), "utf8"));
}
py.FS.mkdirTree("/data");
py.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(home)})`);
const handle = py.pyimport("bridge").handle;

async function call(method, path, { json, form } = {}) {
  const req = new Request("https://demo.test" + path, {
    method,
    headers: json ? { "Content-Type": "application/json" } : undefined,
    body: json ? JSON.stringify(json) : form,
  });
  const body = ["GET", "HEAD"].includes(method) ? null : new Uint8Array(await req.arrayBuffer());
  const res = await handle(method, path, [...req.headers.entries()], body);
  const out = res.toJs({ dict_converter: Object.fromEntries });
  res.destroy();
  const text = new TextDecoder().decode(out.body);
  return { status: out.status, headers: Object.fromEntries(out.headers), text, json: () => JSON.parse(text) };
}

let failures = 0;
function check(label, ok, detail = "") {
  console.log(`${ok ? "ok  " : "FAIL"} ${label}${ok ? "" : "  " + detail}`);
  if (!ok) failures++;
}

const health = (await call("GET", "/api/health")).json();
check("health: mock mode, in browser", health.mode === "mock" && health.in_browser === true, JSON.stringify(health));

const disclosures = (await call("GET", "/api/disclosures")).json();
const application = {
  business_name: "Corporation", tin: "12-3410000", address_line1: "223 Grand St.", city: "New York",
  state: "ny", postal_code: "10013", disclosures_accepted: true, disclosure_version: disclosures.version,
  signature_name: "Jane Doe", products: [{ product_id: "business-checking", initial_deposit_cents: 10000 }],
  owners: [{ name: "Jane Doe", title: "CEO", ownership_pct: 100, control_person: true }],
};
const created = await call("POST", "/api/applications", { json: application });
check("submit application", created.status === 201, created.text);
const id = created.json().id;
check("tax ID masked", created.json().applicant.tin === "*****0000", created.text);

const refreshed = (await call("GET", `/api/applications/${id}?refresh=true`)).json();
check("mock checks finish", refreshed.recommendation === "recommend_approve", JSON.stringify(refreshed.recommendation));

const form = new FormData();
form.append("doc_type", "formation_document");
form.append("file", new Blob(["%PDF-1.4 demo"], { type: "application/pdf" }), "articles.pdf");
const uploaded = await call("POST", `/api/applications/${id}/documents`, { form });
check("upload document", uploaded.status === 201, uploaded.text);
const doc = await call("GET", `/api/applications/${id}/documents/${uploaded.json().id}`);
check("download document", doc.status === 200 && doc.text === "%PDF-1.4 demo", `${doc.status} ${doc.text.slice(0, 200)}`);

const decided = await call("POST", `/api/applications/${id}/decision`, { json: { decision: "approved" } });
check("reviewer approves", decided.json().final_decision === "approved", decided.text);
const exported = await call("GET", `/api/applications/${id}/export`);
check("export application", exported.status === 200 && /attachment/.test(exported.headers["content-disposition"] || ""), exported.text.slice(0, 200));
const audit = await call("GET", "/api/audit.csv");
check("export audit log", audit.status === 200 && audit.text.includes(id), audit.text.slice(0, 200));
check("bad input is rejected", (await call("POST", "/api/applications", { json: { ...application, tin: "123" } })).status === 422);

console.log(failures ? `${failures} check(s) failed` : "All checks passed");
process.exit(failures ? 1 : 0);
