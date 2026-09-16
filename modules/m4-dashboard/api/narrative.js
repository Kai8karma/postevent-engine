// Vercel serverless function (Node runtime, zero npm deps).
//
//   GET /api/narrative?run_id=<run_id>[&refresh=1][&live=0]
//     -> proxies to ${MODULE_API_URL}/narrative/<run_id>[?refresh=1&live=0]
//        with `Authorization: Bearer ${MODULE_API_TOKEN}` and returns that
//        service's JSON verbatim.
//
// This function makes no model calls of its own. The narrative is written by
// the module API's M4 `analyze` phase (modules/m4-dashboard/dashboard.py,
// see docs/module-api.md's M4 table) and read back out of that run's
// analysis.json; `refresh=1` asks the module API to re-run analyze first.
// Vercel therefore holds no model provider key at all -- only the module
// API's URL and bearer token, both deployment env vars:
//
//   MODULE_API_URL    e.g. https://<service>.up.railway.app  (no trailing path)
//   MODULE_API_TOKEN  the same token the module API and n8n hold
//
// Response shape on success is whatever the module API returned -- currently
// {ok, run_id, refreshed, narrative, source, generated_at, model,
//  validator_disagreements, notes}. Failures answer with {error: "..."} and a
// 4xx/5xx status; the dashboard page falls back to its own rendered narrative
// on any non-2xx, so this function fails closed without breaking the page.

const http = require("http");
const https = require("https");
const { URL } = require("url");

const UPSTREAM_TIMEOUT_MS = 25000; // vercel.json caps the function at 30s

function queryOf(req) {
  if (req.query && typeof req.query === "object") return req.query;
  try {
    const parsed = new URL(req.url, "http://localhost");
    const out = {};
    parsed.searchParams.forEach((value, key) => { out[key] = value; });
    return out;
  } catch (e) {
    return {};
  }
}

function isTruthy(value) {
  return ["1", "true", "yes"].indexOf(String(value === undefined ? "" : value).toLowerCase()) !== -1;
}

function isFalsy(value) {
  return ["0", "false", "no"].indexOf(String(value === undefined ? "" : value).toLowerCase()) !== -1;
}

// GET <base>/narrative/<run_id><search>, Bearer token in the header only (it
// is never placed in the URL, logged, or echoed into a response).
function fetchNarrative(base, runId, search, token) {
  return new Promise((resolve, reject) => {
    let target;
    try {
      target = new URL(base.replace(/\/+$/, "") + "/narrative/" + encodeURIComponent(runId) + search);
    } catch (e) {
      reject(new Error("MODULE_API_URL is not a valid URL"));
      return;
    }
    const transport = target.protocol === "http:" ? http : https;
    const headers = { accept: "application/json" };
    if (token) headers.authorization = "Bearer " + token;

    const request = transport.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === "http:" ? 80 : 443),
        path: target.pathname + target.search,
        method: "GET",
        headers: headers,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => resolve({ status: res.statusCode, body: data }));
      }
    );
    request.on("error", (err) => reject(err));
    request.setTimeout(UPSTREAM_TIMEOUT_MS, () => {
      request.destroy(new Error("module API did not answer within " + UPSTREAM_TIMEOUT_MS + "ms"));
    });
    request.end();
  });
}

module.exports = async (req, res) => {
  if (req.method !== "GET" && req.method !== "POST") {
    res.status(405).json({ error: "method not allowed" });
    return;
  }

  const base = String(process.env.MODULE_API_URL || "").trim();
  if (!base) {
    res.status(503).json({ error: "MODULE_API_URL not configured" });
    return;
  }

  const query = queryOf(req);
  const runId = String(query.run_id || "").trim();
  if (!runId) {
    res.status(400).json({ error: "run_id is required (GET /api/narrative?run_id=<run_id>)" });
    return;
  }

  // Only the two query params the module API's own endpoint documents are
  // forwarded; anything else the page appends is ignored rather than passed
  // through blindly.
  const forwarded = [];
  if (isTruthy(query.refresh)) forwarded.push("refresh=1");
  if (isFalsy(query.live)) forwarded.push("live=0");
  const search = forwarded.length ? "?" + forwarded.join("&") : "";

  let upstream;
  try {
    upstream = await fetchNarrative(base, runId, search, String(process.env.MODULE_API_TOKEN || "").trim());
  } catch (err) {
    res.status(504).json({ error: "module API request failed: " + String((err && err.message) || err).slice(0, 200) });
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(upstream.body);
  } catch (e) {
    res.status(502).json({
      error: "module API returned a non-JSON body",
      upstream_status: upstream.status,
      upstream_body: String(upstream.body || "").slice(0, 300),
    });
    return;
  }
  // Verbatim: the module API is the only thing that decides what the
  // narrative says, including when it says the refresh failed.
  res.status(upstream.status).json(parsed);
};
