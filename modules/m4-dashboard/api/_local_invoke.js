// Local harness for modules/m4-dashboard/api/narrative.js: invokes the Vercel
// handler with a fake (req, res) pair against a module API you are already
// running, and prints the JSON it returns. No model provider is involved --
// the handler is a proxy, so this exercises the same path production takes
// (Vercel -> module API -> that run's analysis.json).
//
// Usage:
//   # terminal 1 -- the module API this proxies to
//   MODULE_API_TOKEN=t python3 api/server.py --port 8080
//
//   # terminal 2 -- one run_id that already has an M4 analyze phase on disk
//   MODULE_API_URL=http://127.0.0.1:8080 MODULE_API_TOKEN=t \
//     node modules/m4-dashboard/api/_local_invoke.js <run_id> [--refresh] [--offline]
//
// --refresh asks the module API to re-run `analyze` before answering;
// --offline adds live=0 so that re-run stays on the offline lane (zero
// network, zero spend). Neither env var is ever printed.
//
// Exits 0 on a 2xx response, 1 otherwise (no fabricated output either way).

const handler = require("./narrative.js");

const args = process.argv.slice(2);
const runId = args.filter((a) => !a.startsWith("--"))[0];
const refresh = args.indexOf("--refresh") !== -1;
const offline = args.indexOf("--offline") !== -1;

if (!runId) {
  console.error("usage: node _local_invoke.js <run_id> [--refresh] [--offline]");
  process.exit(1);
}
if (!process.env.MODULE_API_URL) {
  console.error("note: MODULE_API_URL is unset -- the handler will answer 503, which is the documented behaviour");
}

const query = { run_id: runId };
if (refresh) query.refresh = "1";
if (offline) query.live = "0";

const search = Object.keys(query).map((k) => k + "=" + encodeURIComponent(query[k])).join("&");
const req = { method: "GET", url: "/api/narrative?" + search, query: query };

function makeRes(onDone) {
  return {
    _status: 200,
    status(code) { this._status = code; return this; },
    json(obj) { onDone(this._status, obj); return this; },
  };
}

const startedAt = Date.now();
const res = makeRes((statusCode, body) => {
  console.log(JSON.stringify({ http_status: statusCode, elapsed_ms: Date.now() - startedAt, body: body }, null, 2));
  if (statusCode < 200 || statusCode >= 300) {
    process.exitCode = 1;
  }
});

Promise.resolve(handler(req, res)).catch((err) => {
  console.error(JSON.stringify({
    elapsed_ms: Date.now() - startedAt,
    error: String((err && err.message) || err),
  }, null, 2));
  process.exitCode = 1;
});
