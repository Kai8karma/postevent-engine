// Vercel serverless function (Node runtime, zero npm deps).
// POST /api/narrative  { event, host_company, kpis, top_accounts, committee_account_count, anomalies }
// -> 200 { paragraphs: [p1, p2], generated_at, source: "live" }
//
// Reads ANTHROPIC_API_KEY from the environment and calls the Anthropic
// Messages API (model: claude-sonnet-5) to write a fresh two-paragraph
// GTM-analyst narrative from the dashboard's own computed stats. If
// ANTHROPIC_API_KEY is not set but OPENROUTER_API_KEY is, falls back to
// OpenRouter's chat-completions API with the same prompt/contract and a
// Claude Sonnet model (see OPENROUTER_MODEL_FALLBACKS below; override with
// OPENROUTER_MODEL). The dashboard client applies its own 25s timeout and
// falls back to the baked-in fallback_narrative.md content on any error
// here, so this function can fail closed without breaking the page.

const https = require("https");

// Verified live against https://openrouter.ai/api/v1/models on 2026-08-23:
// anthropic/claude-3.7-sonnet and anthropic/claude-3.5-sonnet no longer
// exist on OpenRouter (404), so this uses the three current Anthropic
// Sonnet ids instead, newest first. Tried in order on 400/404 model errors.
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const OPENROUTER_MODEL_FALLBACKS = [
  "anthropic/claude-sonnet-5",
  "anthropic/claude-sonnet-4.6",
  "anthropic/claude-sonnet-4.5",
];

const SYSTEM_PROMPT =
  "You are a sharp GTM/RevOps analyst writing the narrative panel of a " +
  "post-event lead-intelligence dashboard. You will be given computed " +
  "stats as JSON: KPIs, top engaged accounts, buying-committee counts, " +
  "and anomaly callouts. Respond with STRICT JSON only, no prose outside " +
  "it, in exactly this shape: {\"paragraphs\": [\"paragraph one\", " +
  "\"paragraph two\"]}. Paragraph one covers the funnel/top-accounts/" +
  "buying-committee picture, naming the actual top account(s) by name " +
  "and their coverage numbers. Paragraph two covers the anomaly " +
  "callout(s) by contact and company name and what action it implies. " +
  "Be concrete with the numbers given, do not invent new ones, and do " +
  "not exceed two paragraphs.";

function readBody(req) {
  return new Promise((resolve, reject) => {
    if (req.body && typeof req.body === "object") {
      // Vercel's Node runtime usually pre-parses JSON bodies onto req.body.
      resolve(req.body);
      return;
    }
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      if (!raw) { resolve({}); return; }
      try { resolve(JSON.parse(raw)); }
      catch (e) { resolve({}); }
    });
    req.on("error", reject);
  });
}

function callAnthropic(apiKey, prompt) {
  const payload = JSON.stringify({
    model: "claude-sonnet-5",
    max_tokens: 700,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: prompt }],
  });

  const options = {
    hostname: "api.anthropic.com",
    path: "/v1/messages",
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      "content-length": Buffer.byteLength(payload),
    },
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error("anthropic status " + res.statusCode + ": " + data.slice(0, 300)));
          return;
        }
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error("bad json from anthropic: " + e.message)); }
      });
    });
    req.on("error", reject);
    req.setTimeout(9000, () => req.destroy(new Error("anthropic request timed out")));
    req.write(payload);
    req.end();
  });
}

function extractParagraphsFromText(text) {
  text = (text || "").trim();
  if (!text) throw new Error("empty completion");

  // Model is asked for strict JSON; be tolerant of stray wrapping text (and
  // ```json fences) anyway.
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start !== -1 && end !== -1 && end > start) {
    try {
      const parsed = JSON.parse(text.slice(start, end + 1));
      if (Array.isArray(parsed.paragraphs) && parsed.paragraphs.length) {
        return parsed.paragraphs.map((p) => String(p).trim()).filter(Boolean);
      }
    } catch (e) { /* fall through to plain-text split */ }
  }
  const paras = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  if (!paras.length) throw new Error("could not parse narrative paragraphs");
  return paras;
}

function extractParagraphs(anthropicResponse) {
  const block = (anthropicResponse.content || []).find((b) => b.type === "text");
  const text = block && block.text ? block.text.trim() : "";
  return extractParagraphsFromText(text);
}

// --- OpenRouter fallback (used when ANTHROPIC_API_KEY is unset) ---

function callOpenRouterModel(apiKey, model, content) {
  const payload = JSON.stringify({
    model: model,
    messages: [{ role: "user", content: content }],
    temperature: 0.2,
    max_tokens: 4000,
  });

  const options = {
    hostname: "openrouter.ai",
    path: "/api/v1/chat/completions",
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer " + apiKey,
      "http-referer": "https://kai8karma.github.io/agentkai/",
      "x-title": "Post-Event Engine",
      "content-length": Buffer.byteLength(payload),
    },
  };

  function attempt(retriesLeft) {
    return new Promise((resolve, reject) => {
      const req = https.request(options, (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              const parsed = JSON.parse(data);
              const text = parsed.choices && parsed.choices[0] && parsed.choices[0].message
                ? parsed.choices[0].message.content : "";
              resolve(text);
            } catch (e) { reject(new Error("bad json from openrouter: " + e.message)); }
            return;
          }
          if (res.statusCode === 400 || res.statusCode === 404) {
            const err = new Error("openrouter model " + model + " rejected (HTTP " + res.statusCode + "): " + data.slice(0, 300));
            err.isModelError = true;
            reject(err);
            return;
          }
          if ((res.statusCode === 429 || res.statusCode >= 500) && retriesLeft > 0) {
            resolve(attempt(retriesLeft - 1));
            return;
          }
          reject(new Error("openrouter status " + res.statusCode + ": " + data.slice(0, 300)));
        });
      });
      req.on("error", reject);
      req.setTimeout(60000, () => req.destroy(new Error("openrouter request timed out")));
      req.write(payload);
      req.end();
    });
  }

  return attempt(1);
}

function openrouterModelsToTry() {
  const envModel = (process.env.OPENROUTER_MODEL || "").trim();
  if (envModel) {
    return [envModel].concat(OPENROUTER_MODEL_FALLBACKS.filter((m) => m !== envModel));
  }
  return OPENROUTER_MODEL_FALLBACKS.slice();
}

async function callOpenRouter(apiKey, content) {
  let lastErr = null;
  for (const model of openrouterModelsToTry()) {
    try {
      return await callOpenRouterModel(apiKey, model, content);
    } catch (e) {
      if (e.isModelError) { lastErr = e; continue; }
      throw e;
    }
  }
  throw lastErr || new Error("openrouter: no candidate models available");
}

module.exports = async (req, res) => {
  if (req.method !== "POST" && req.method !== "GET") {
    res.status(405).json({ error: "method not allowed" });
    return;
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  const openrouterKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey && !openrouterKey) {
    res.status(500).json({ error: "ANTHROPIC_API_KEY not configured (set OPENROUTER_API_KEY as a fallback)" });
    return;
  }

  try {
    const stats = req.method === "POST" ? await readBody(req) : (req.query || {});
    const prompt = "Dashboard stats:\n" + JSON.stringify(stats, null, 2);
    let paragraphs;
    if (apiKey) {
      const completion = await callAnthropic(apiKey, prompt);
      paragraphs = extractParagraphs(completion);
    } else {
      const text = await callOpenRouter(openrouterKey, SYSTEM_PROMPT + "\n\n" + prompt);
      paragraphs = extractParagraphsFromText(text);
    }
    res.status(200).json({
      paragraphs,
      generated_at: new Date().toISOString(),
      source: "live",
    });
  } catch (err) {
    res.status(502).json({ error: String((err && err.message) || err) });
  }
};
