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
    } catch (e) { /* fall through to truncation salvage, then plain-text split */ }
  }
  // Truncation salvage: a completion cut off at max_tokens leaves unclosed
  // JSON that the strict parse above rejects. If the text still declares a
  // "paragraphs" array, recover its complete string literals instead of
  // rendering the raw JSON blob as narrative text (observed live 2026-08-24).
  if (text.includes('"paragraphs"')) {
    const salvaged = [];
    const re = /"((?:[^"\\]|\\.){80,})"/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      try { salvaged.push(JSON.parse('"' + m[1] + '"').trim()); }
      catch (e) { /* skip literals with bad escapes */ }
    }
    if (salvaged.length) return salvaged;
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

function callOpenRouterModel(apiKey, model, content, maxTokens) {
  const payload = JSON.stringify({
    model: model,
    messages: [{ role: "user", content: content }],
    temperature: 0.2,
    // Was 4000 (402-risk: OpenRouter rejects requests the account can't
    // afford at max_tokens, observed at 1672 affordable), then 800 — which
    // the free nemotron chain overflowed live on 2026-08-24, truncating the
    // JSON mid-paragraph and breaking the strict parse. 1600 fits two
    // paragraphs + JSON overhead and stays under the observed afford line.
    // callOpenRouter may retry once with a smaller, affordability-clamped
    // value parsed from a 402 body ("can only afford N") — see below.
    max_tokens: maxTokens || 1600,
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

// A 402 body states exactly how many tokens the account can still afford
// ("You requested up to 1600 tokens, but can only afford 285"). Below
// MIN_USEFUL_TOKENS a two-paragraph answer would truncate into unparseable
// JSON, so we don't bother retrying under that floor.
const MIN_USEFUL_TOKENS = 450;

function affordableFrom402(message) {
  const m = /can only afford (\d+)/.exec(String(message || ""));
  return m ? parseInt(m[1], 10) : null;
}

async function callOpenRouter(apiKey, content) {
  let lastErr = null;
  for (const model of openrouterModelsToTry()) {
    try {
      return await callOpenRouterModel(apiKey, model, content);
    } catch (e) {
      // Quota-aware retry: a 402 names the affordable token count. If it's
      // still enough for a real answer, retry this model once with the
      // clamped ceiling instead of failing the whole chain.
      const affordable = affordableFrom402(e && e.message);
      if (affordable !== null && affordable - 64 >= MIN_USEFUL_TOKENS) {
        try {
          return await callOpenRouterModel(apiKey, model, content, affordable - 64);
        } catch (e2) {
          lastErr = e2;
          continue;
        }
      }
      if (e.isModelError || affordable !== null) { lastErr = e; continue; }
      throw e;
    }
  }
  throw lastErr || new Error("openrouter: no candidate models available");
}

// --- Additional free-tier providers (each used only when its key is set) ---
// One shared https helper: OpenAI-compatible chat-completions shape is used by
// Groq and Sarvam; Gemini has its own generateContent shape. Each provider has
// an independent quota pool, so one provider's daily death no longer takes the
// live narrative down with it.

function httpsJson(hostname, path, headers, payloadObj) {
  const https = require("https");
  const payload = JSON.stringify(payloadObj);
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname, path, method: "POST",
      headers: Object.assign({ "content-type": "application/json", "content-length": Buffer.byteLength(payload) }, headers),
    }, (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try { resolve(JSON.parse(data)); } catch (e) { reject(new Error(hostname + " unparseable response: " + data.slice(0, 200))); }
        } else {
          reject(new Error(hostname + " status " + res.statusCode + ": " + data.slice(0, 300)));
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(20000, () => { req.destroy(new Error(hostname + " timeout")); });
    req.write(payload);
    req.end();
  });
}

async function callGemini(apiKey, content) {
  const model = (process.env.GEMINI_MODEL || "gemini-2.0-flash").trim();
  const out = await httpsJson(
    "generativelanguage.googleapis.com",
    "/v1beta/models/" + model + ":generateContent",
    { "x-goog-api-key": apiKey },
    { contents: [{ parts: [{ text: content }] }], generationConfig: { temperature: 0.2, maxOutputTokens: 1600 } }
  );
  const parts = (((out.candidates || [])[0] || {}).content || {}).parts || [];
  const text = parts.map((p) => p.text || "").join("").trim();
  if (!text) throw new Error("gemini: empty completion");
  return text;
}

async function callOpenAICompatible(hostname, path, apiKey, model, content) {
  const out = await httpsJson(
    hostname, path,
    { authorization: "Bearer " + apiKey },
    { model, messages: [{ role: "user", content }], temperature: 0.2, max_tokens: 1600 }
  );
  const text = ((((out.choices || [])[0] || {}).message || {}).content || "").trim();
  if (!text) throw new Error(hostname + ": empty completion");
  return text;
}

// Provider chain: try every configured provider in order; first parseable
// answer wins. Errors accumulate for the llm_error field.
async function callAnyProvider(content) {
  const attempts = [];
  const errors = [];
  if (process.env.OPENROUTER_API_KEY) attempts.push(["openrouter", () => callOpenRouter(process.env.OPENROUTER_API_KEY, content)]);
  if (process.env.GEMINI_API_KEY) attempts.push(["gemini", () => callGemini(process.env.GEMINI_API_KEY, content)]);
  if (process.env.GROQ_API_KEY) attempts.push(["groq", () => callOpenAICompatible("api.groq.com", "/openai/v1/chat/completions", process.env.GROQ_API_KEY, (process.env.GROQ_MODEL || "llama-3.3-70b-versatile").trim(), content)]);
  if (process.env.SARVAM_API_KEY) attempts.push(["sarvam", () => callOpenAICompatible("api.sarvam.ai", "/v1/chat/completions", process.env.SARVAM_API_KEY, (process.env.SARVAM_MODEL || "sarvam-m").trim(), content)]);
  for (const [name, fn] of attempts) {
    try {
      const text = await fn();
      const paragraphs = extractParagraphsFromText(text);
      return { paragraphs, provider: name };
    } catch (e) {
      errors.push(name + ": " + String((e && e.message) || e).slice(0, 140));
    }
  }
  throw new Error(errors.length ? errors.join(" | ") : "no LLM provider keys configured");
}

// --- Deterministic fallback: composed from the caller's own numbers, no
// model involved. Runs when every LLM lane fails (quota, network, parse).
// Honest by construction: every figure comes straight from the payload the
// dashboard sent, and the client labels the result as deterministic.

function deterministicNarrative(stats) {
  const s = (stats && (stats.summary || stats)) || {};
  const sentences1 = [];
  if (s.total_attendees !== undefined && s.attendee_to_mql_pct !== undefined) {
    sentences1.push("Of " + s.total_attendees + " attendees, " + s.attendee_to_mql_pct + "% have converted to MQL or beyond.");
  } else if (s.total_attendees !== undefined) {
    sentences1.push(s.total_attendees + " attendees are being tracked for this event.");
  }
  const w = s.windows || {};
  const windowKeys = Object.keys(w);
  if (windowKeys.length) {
    const parts = windowKeys.sort((a, b) => Number(a) - Number(b)).map((k) => w[k] + " within " + k + " days");
    sentences1.push("Lifecycle movement: " + parts.join(", ") + ".");
  }
  const sentences2 = [];
  const anomalies = (stats && stats.anomalies) || [];
  if (anomalies.length) {
    sentences2.push(anomalies.length + " engagement anomal" + (anomalies.length === 1 ? "y" : "ies") + " flagged by the deterministic detector — see the anomaly cards above for the specifics.");
  } else {
    sentences2.push("No engagement anomalies are flagged in the current data.");
  }
  const tops = (stats && stats.top_accounts) || [];
  if (tops.length) {
    sentences2.push("Top engaged accounts are listed in the table above; review the highest-scored rows for buying-committee coverage.");
  }
  sentences2.push("This summary was composed deterministically from the dashboard's own figures because the live AI lane was unavailable at load time.");
  const p1 = sentences1.join(" ") || "Dashboard figures are shown above; no summary statistics were included in this request.";
  return [p1, sentences2.join(" ")];
}

module.exports = async (req, res) => {
  if (req.method !== "POST" && req.method !== "GET") {
    res.status(405).json({ error: "method not allowed" });
    return;
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;

  let stats = {};
  try {
    stats = req.method === "POST" ? await readBody(req) : (req.query || {});
    const prompt = "Dashboard stats:\n" + JSON.stringify(stats, null, 2);
    let paragraphs;
    let provider = "anthropic";
    if (apiKey) {
      const completion = await callAnthropic(apiKey, prompt);
      paragraphs = extractParagraphs(completion);
    } else {
      const result = await callAnyProvider(SYSTEM_PROMPT + "\n\n" + prompt);
      paragraphs = result.paragraphs;
      provider = result.provider;
    }
    res.status(200).json({
      paragraphs,
      generated_at: new Date().toISOString(),
      source: "live",
      provider,
    });
  } catch (err) {
    // Every LLM lane failed (quota 402s, network, unparseable output).
    // Respond 200 with a deterministic summary built from the caller's own
    // numbers rather than 502-ing the page into a stale baked cache: the
    // figures stay current even when no model is reachable, and the client
    // shows a distinct "deterministic" badge. llm_error keeps the real
    // failure visible for anyone who curls the endpoint.
    res.status(200).json({
      paragraphs: deterministicNarrative(stats),
      generated_at: new Date().toISOString(),
      source: "deterministic",
      llm_error: String((err && err.message) || err).slice(0, 300),
    });
  }
};
