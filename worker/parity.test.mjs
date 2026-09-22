import { typedQuestion } from "./index.js";
import { execFileSync } from "node:child_process";
const qs = [
  "Should Trilumi add a CMS?", "Is the homepage copy too salesy?",
  "Which fits best: Astro, Next.js, or plain HTML?", "Should we redesign the site or leave it as is?",
  "Which should we use, Astro or Next.js?", "Is it better to rebuild or patch?",
  "Which is faster to build with Astro or Next.js?", "How risky is migrating this week?",
  "How likely is it that the client signs?", "How hard is adding a CMS?", "How ready is this for launch?",
  "Do we pick email, LinkedIn, and cold calls, or just email?", "Can we ship on Friday?",
  "What is the best colour for the button?", "Why is the site slow?",
  "Which should Trilumi build first: a CMS, a new token landing page, or an SEO pass?",
];
let ok = 0, bad = 0;
for (const q of qs) {
  let js, py;
  try { js = JSON.stringify(typedQuestion(q, null, [], [])[0]); } catch (e) { js = "ERR"; }
  try { py = JSON.stringify(JSON.parse(execFileSync("python3", ["ask_jev.py", q, "--dry-run"], { cwd: new URL("..", import.meta.url).pathname, stdio: ["ignore", "pipe", "ignore"] }).toString()).questions.q1); } catch (e) { py = "ERR"; }
  const same = js === py; same ? ok++ : bad++;
  console.log((same ? "OK  " : "DIFF") + " " + q + (same ? "" : `\n   js: ${js}\n   py: ${py}`));
}
console.log(`\n${ok} match, ${bad} differ`);
