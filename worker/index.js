/**
 * Ask JEV — Cloudflare Worker.
 *
 * Serves ui/index.html (as a static asset) and proxies /api/ask and /api/preview to
 * TypeSafe with the owner's key, which lives in the TYPESAFE_API_KEY secret and never
 * reaches the browser. The question parser below is a line-for-line port of ask_jev.py;
 * keep the two in step.
 *
 *   npx wrangler secret put TYPESAFE_API_KEY
 *   npx wrangler deploy
 */

const API_URL = "https://api.typesafe.ai/v1/systemone";
const MODEL = "jev-latest";

// Abuse caps: the key is the owner's, so keep one request small and one visitor slow.
const MAX_QUESTIONS = 5;
const MAX_QUESTION_CHARS = 500;
const MAX_CONTEXT_CHARS = 6000;

const YES_ABOVE = 0.7, NO_BELOW = 0.3, CLOSE_CALL_CONFIDENCE = 0.5;

class JevError extends Error {}

// ------------------------------------------------ natural language -> typed ----

const AUX = "(?:is|are|am|was|were|do|does|did|should|shall|can|could|will|would|has|have|had|must|may|might|need|needs|ought)";
const YESNO_RE = new RegExp(`^\\s*${AUX}\\b`, "i");
const DEGREE_RE = /^\s*how\s+(\w+)/i;
const WHICH_RE = /^\s*(?:which|what)\b/i;
const OR_RE = /\s+or\s+/i;
const STEM_RE = new RegExp(`^\\s*${AUX}\\s+(?:we|i|you|they|it|one|the team)\\s+(?:(?:be\\s+)?(?:better|best|wiser|smarter|safer)\\s+)?(?:to\\s+)?`, "i");

// Score levels describe situations, not degrees. Keyed by the adjective after "how ...".
const LEVEL_SETS = [
  [["likely", "probable", "plausible", "realistic"], [
    "Almost certainly will not happen",
    "Unlikely; would need something unusual to go right",
    "Could go either way",
    "Likely; the normal outcome unless something goes wrong",
    "Almost certain to happen",
  ]],
  [["risky", "dangerous", "safe", "unsafe"], [
    "Nothing meaningful can go wrong",
    "Small, easily reversible problems are possible",
    "Real chance of a setback that costs time or money to undo",
    "Serious harm is likely unless it is actively mitigated",
    "Likely to cause severe or irreversible damage",
  ]],
  [["hard", "difficult", "complex", "complicated", "easy", "simple", "big", "large"], [
    "Trivial; minutes of routine work",
    "Straightforward; a known recipe, under a day",
    "Moderate; several days with some unknowns",
    "Hard; weeks of work, needs expertise, real unknowns",
    "Very hard; months of work or may not be achievable",
  ]],
  [["urgent", "important", "critical", "pressing"], [
    "Can wait indefinitely at no cost",
    "Nice to have soon; waiting weeks costs nothing",
    "Should be done within days; waiting has a modest cost",
    "Needed today; delay causes real harm",
    "Critical right now; every hour of delay causes damage",
  ]],
  [["good", "strong", "well", "ready", "mature", "solid", "polished", "healthy", "effective", "clear", "bad", "weak"], [
    "Poor; fails at its basic purpose",
    "Weak; works but with major gaps",
    "Adequate; does the job with noticeable rough edges",
    "Good; solid with only minor gaps",
    "Excellent; hard to improve",
  ]],
  [["valuable", "useful", "worthwhile", "profitable", "much", "many"], [
    "No value; nothing gained",
    "Marginal value; barely worth the effort",
    "Useful; a clear but modest gain",
    "High value; a significant gain",
    "Transformative; changes the outcome entirely",
  ]],
  [[], ["Not at all", "A little", "A moderate amount", "A lot", "An extreme amount"]],
];

function levelsFor(adjective) {
  const adj = adjective.toLowerCase();
  for (const [words, levels] of LEVEL_SETS) if (words.includes(adj)) return levels;
  return LEVEL_SETS[LEVEL_SETS.length - 1][1];
}

/** Pull "A, B, or C" style options out of a sentence. Empty array if none found. */
function splitOptions(question) {
  const body = question.trim().replace(/[?.!\s]+$/, "").trim();
  if (!OR_RE.test(body) && !body.includes(",")) return [];

  let seg, m;
  if (body.includes(":")) {
    seg = body.slice(body.indexOf(":") + 1);
  } else if ((m = body.match(/\b(?:between|among|out of)\s+(.+)$/i))) {
    seg = m[1];
  } else if (WHICH_RE.test(body) && body.includes(",")) {
    seg = body.slice(body.indexOf(",") + 1);
  } else {
    seg = body.replace(STEM_RE, "");
    if (WHICH_RE.test(seg)) {
      const firstOr = seg.match(OR_RE);
      if (!firstOr) return [];
      const prefix = seg.slice(0, firstOr.index);
      const leads = [...prefix.matchAll(/\b(?:is|are|be|to|use|using|choose|pick|prefer|with|take|go|do|for)\s+/gi)];
      if (!leads.length) return [];
      const last = leads[leads.length - 1];
      seg = seg.slice(last.index + last[0].length);
    }
  }

  const opts = seg.trim().split(/\s*,\s*(?:or|and)\s+|\s+or\s+|\s*,\s*/i)
    .map((p) => p.replace(/^[\s"']+|[\s"']+$/g, ""))
    .filter(Boolean)
    .map((o) => o.replace(/^(?:rather|instead|just|simply)\s+/i, ""));
  if (opts.length < 2 || opts.length > 20 || opts.some((o) => o.length > 80)) return [];
  return opts;
}

/** Returns [question object for the API, note about how it was interpreted]. */
function typedQuestion(question, force, options, levels) {
  const q = question.trim();
  const criteriaOf = (opts) => Object.fromEntries(opts.map((o) => [o, null]));
  if (options && options.length) return [{ type: "choice", instructions: q, criteria: criteriaOf(options) }, "choice (options given)"];
  if (levels && levels.length) return [{ type: "score", instructions: q, criteria: levels }, "score (levels given)"];

  let kind = force || null, m;
  if (!kind) {
    if ((m = q.match(DEGREE_RE))) {
      kind = "score"; levels = levelsFor(m[1]);
    } else if (YESNO_RE.test(q) && !WHICH_RE.test(q)) {
      const found = splitOptions(q); kind = found.length ? "choice" : "noul"; options = found;
    } else if (WHICH_RE.test(q)) {
      options = splitOptions(q); kind = options.length ? "choice" : null;
    }
  }

  if (kind === "noul") return [{ type: "noul", instructions: q }, "yes/no"];
  if (kind === "choice") {
    options = options && options.length ? options : splitOptions(q);
    if (!options.length) throw new JevError("Could not find the options in that question. List them in Advanced → Options, or phrase it like 'X, Y, or Z?'");
    return [{ type: "choice", instructions: q, criteria: criteriaOf(options) }, "pick one"];
  }
  if (kind === "score") {
    levels = levels && levels.length ? levels : levelsFor((q.match(DEGREE_RE) || [null, "much"])[1]);
    return [{ type: "score", instructions: q, criteria: levels }, "how much (default levels; set Levels for sharper ones)"];
  }
  throw new JevError("Jev answers yes/no, pick-one, or how-much questions only; it does not write explanations.\n" +
    "Rephrase, e.g.  'Is X true?'  |  'A, B, or C?'  |  'How risky is X?'\n" +
    "or force a shape in Advanced with Options / Levels / Shape.");
}

// ----------------------------------------------------------------- answers ----

const f2 = (x) => Number(x).toFixed(2);

function readAnswer(ans) {
  if (ans.type === "noul") {
    const p = ans.noul;
    const verdict = p >= YES_ABOVE ? "YES" : p <= NO_BELOW ? "NO" : "UNSURE";
    return [`${verdict} (p(yes)=${f2(p)})`, ""];
  }
  if (ans.type === "choice") {
    const tag = ans.confidence >= CLOSE_CALL_CONFIDENCE ? "" : ", close call";
    const dist = Object.entries(ans.probabilities).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${f2(v)}`).join(" | ");
    return [`${ans.choice} (p=${f2(ans.probabilities[ans.choice])}, confidence ${f2(ans.confidence)}${tag})`, `options: ${dist}`];
  }
  if (ans.type === "score") {
    const keys = Object.keys(ans.legend);
    const top = keys.length - 1;
    const nearest = ans.legend[String(Math.round(ans.score))];
    const tag = ans.confidence >= CLOSE_CALL_CONFIDENCE ? "" : ", close call";
    const dist = keys.map((i) => `[${i}] ${ans.legend[i].slice(0, 38)} ${f2(ans.probabilities[i])}`).join(" | ");
    return [`${f2(ans.score)} on 0-${top} = "${nearest}" (confidence ${f2(ans.confidence)}${tag})`, `levels: ${dist}`];
  }
  return [JSON.stringify(ans), ""];
}

// ----------------------------------------------------------------- request ----

const splitCsv = (text) => (text || "").split(",").map((t) => t.trim()).filter(Boolean);

function parseBody(body) {
  const questions = (Array.isArray(body.questions) ? body.questions : []).map((q) => String(q).trim()).filter(Boolean);
  if (!questions.length) throw new JevError("Type a question first.");
  if (questions.length > MAX_QUESTIONS) throw new JevError(`At most ${MAX_QUESTIONS} questions per call on the public demo.`);
  if (questions.some((q) => q.length > MAX_QUESTION_CHARS)) throw new JevError(`Keep each question under ${MAX_QUESTION_CHARS} characters.`);
  const context = String(body.context || "").trim();
  if (context.length > MAX_CONTEXT_CHARS) throw new JevError(`Keep the context under ${MAX_CONTEXT_CHARS} characters on the public demo.`);
  const options = splitCsv(body.options);
  const levels = splitCsv(body.levels);
  if (levels.length && (levels.length < 2 || levels.length > 10)) throw new JevError("Levels needs between 2 and 10 entries.");
  const force = ["noul", "choice", "score"].includes(body.type) ? body.type : null;

  const state = context ? { notes: context }
    : { context: "No specific context was supplied. Judge from the question alone and general knowledge." };
  const typed = {}, shapes = [];
  questions.forEach((q, i) => { const [tq, note] = typedQuestion(q, force, options, levels); typed[`q${i + 1}`] = tq; shapes.push(note); });
  return { questions, state, typed, shapes };
}

async function callJev(state, questions, apiKey) {
  let delay = 500;
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(API_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ state, model: MODEL, questions }),
    });
    if (res.ok) return res.json();
    const detail = await res.text();
    if ((res.status === 429 || res.status === 529) && attempt < 2) { await new Promise((r) => setTimeout(r, delay)); delay *= 2; continue; }
    if (res.status === 401) throw new JevError("The TypeSafe key on this deployment is missing or invalid — the owner needs to fix it.");
    if (res.status === 422) throw new JevError(`TypeSafe rejected the question: ${detail}`);
    throw new JevError(`TypeSafe is busy (${res.status}). Try again in a moment.`);
  }
  throw new JevError("TypeSafe is busy. Try again in a moment.");
}

const json = (payload, status = 200) => new Response(JSON.stringify(payload), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
});

async function handleApi(request, env, path) {
  if (request.method === "GET") {
    if (path === "/api/history") return json({ client: true, public: true });
    if (path === "/api/contexts") return json([]);
    return json({ error: "not found" }, 404);
  }
  if (request.method !== "POST" || (path !== "/api/ask" && path !== "/api/preview")) return json({ error: "not found" }, 404);

  let body;
  try { body = await request.json(); } catch { return json({ error: "Body must be JSON." }, 400); }

  try {
    const { questions, state, typed, shapes } = parseBody(body);
    if (path === "/api/preview") return json({ request: { state, model: MODEL, questions: typed }, shapes });

    // One key for everyone: slow each visitor down, and cap the whole deployment.
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const [perIp, global] = await Promise.all([env.IP_LIMIT.limit({ key: ip }), env.GLOBAL_LIMIT.limit({ key: "all" })]);
    if (!perIp.success) return json({ error: "Slow down — you have hit the per-visitor limit. Try again in a minute." }, 429);
    if (!global.success) return json({ error: "The public demo is busy right now. Try again in a minute." }, 429);
    if (!env.TYPESAFE_API_KEY) throw new JevError("The TypeSafe key is not configured on this deployment.");

    const resp = await callJev(state, typed, env.TYPESAFE_API_KEY);
    const results = questions.map((q, i) => {
      const answer = resp.answers[`q${i + 1}`];
      const [verdict, detail] = readAnswer(answer);
      return { question: q, shape: shapes[i], verdict, detail, typed_question: typed[`q${i + 1}`], answer };
    });
    return json({ model: resp.model, usage: resp.usage, state, results });
  } catch (e) {
    if (e instanceof JevError) return json({ error: e.message }, 400);
    console.error(e);
    return json({ error: `Something went wrong: ${e.message}` }, 500);
  }
}

export { splitOptions, typedQuestion, readAnswer };   // for tests

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname.startsWith("/api/")) return handleApi(request, env, pathname);
    return json({ error: "not found" }, 404);   // everything else is served from ui/ as a static asset
  },
};
