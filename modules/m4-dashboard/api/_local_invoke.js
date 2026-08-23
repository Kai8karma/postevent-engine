// Local harness to execute modules/m4-dashboard/api/narrative.js against a
// real provider (Anthropic or OpenRouter, per its own fallback order) with a
// fake minimal (req, res) pair. Prints the JSON response and never prints
// any API key.
//
// Usage:
//   set -a; source ~/.config/postevent/llm.env; set +a
//   OPENROUTER_MODEL="nvidia/nemotron-3-ultra-550b-a55b:free" \
//     node modules/m4-dashboard/api/_local_invoke.js path/to/payload.json
//
// Exits 0 and prints the response JSON to stdout on success.
// Exits 1 and prints the error to stderr on failure (no fabricated output).

const fs = require("fs");
const path = require("path");
const handler = require("./narrative.js");

const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error("usage: node _local_invoke.js <payload.json>");
  process.exit(1);
}
const payload = JSON.parse(fs.readFileSync(path.resolve(payloadPath), "utf8"));

function makeReq(body) {
  return { method: "POST", body: body };
}

function makeRes(onDone) {
  const res = {
    _status: 200,
    status(code) { this._status = code; return this; },
    json(obj) { onDone(this._status, obj); return this; },
  };
  return res;
}

const startedAt = Date.now();
const req = makeReq(payload);
const res = makeRes((statusCode, body) => {
  const elapsedMs = Date.now() - startedAt;
  const result = { http_status: statusCode, elapsed_ms: elapsedMs, body: body };
  console.log(JSON.stringify(result, null, 2));
  if (statusCode < 200 || statusCode >= 300) {
    process.exitCode = 1;
  }
});

Promise.resolve(handler(req, res)).catch((err) => {
  const elapsedMs = Date.now() - startedAt;
  console.error(JSON.stringify({ elapsed_ms: elapsedMs, error: String((err && err.message) || err) }, null, 2));
  process.exitCode = 1;
});
