// Checks that the Worker's parser (a port of ask_jev.py) gives the expected shape and
// options for every phrasing in golden.json, and that Python and JS agree exactly.
//   node worker/parity.test.mjs     (or: npm test)
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { typedQuestion, detectLang } from "./index.js";

const root = new URL("..", import.meta.url).pathname;
const cases = JSON.parse(readFileSync(new URL("./golden.json", import.meta.url), "utf8"));
const LEVELS = JSON.parse(readFileSync(root + "levels.json", "utf8"));

const levelKey = (lang, criteria) =>
  Object.entries(LEVELS[lang] || {}).find(([, v]) => JSON.stringify(v) === JSON.stringify(criteria))?.[0] ?? null;

let failed = 0;
for (const [q, kind, opts, lvl] of cases) {
  let js = null, py = null;
  try { js = typedQuestion(q, null, [], [])[0]; } catch {}
  try {
    const out = execFileSync("python3", ["ask_jev.py", q, "--dry-run"], { cwd: root, stdio: ["ignore", "pipe", "ignore"] });
    py = JSON.parse(out.toString()).questions.q1;
  } catch {}

  const lang = detectLang(q);
  const gotKind = js ? js.type : null;
  const gotOpts = js && js.type === "choice" ? Object.keys(js.criteria) : null;
  const gotLvl = js && js.type === "score" ? levelKey(lang, js.criteria) : null;
  const golden = gotKind === kind && (opts === null || JSON.stringify(gotOpts) === JSON.stringify(opts)) && (lvl === null || gotLvl === lvl);
  const parity = JSON.stringify(js) === JSON.stringify(py);

  if (golden && parity) continue;
  failed++;
  console.log(`FAIL ${q}`);
  if (!golden) console.log(`     expected ${kind} ${opts ? JSON.stringify(opts) : ""} ${lvl || ""}\n     got      ${gotKind} ${gotOpts ? JSON.stringify(gotOpts) : ""} ${gotLvl || ""}`);
  if (!parity) console.log(`     js: ${JSON.stringify(js)}\n     py: ${JSON.stringify(py)}`);
}
console.log(`${cases.length - failed}/${cases.length} passed (golden + python/js parity)`);
process.exit(failed ? 1 : 0);
