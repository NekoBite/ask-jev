# Ask JEV — TypeSafe's Jev as a judgment engine

**Try it: https://jev.ask-jev.workers.dev** — a plain-English question in, a calibrated
typed answer out, in about a second. No signup; it runs on the owner's TypeSafe key and is
rate-limited, so be considerate.


Two tools, one API key:

| Tool | Ask it | Get back |
|---|---|---|
| `ask_jev.py` | one plain-English question (or several) | a one-line typed answer, fed straight into Claude via `/jev` |
| `jev_brainstorm.py` | a full decision spec (`decisions/*.json`) | a ranked, re-weightable recommendation |

## ask_jev.py — ask JEV in plain English

```bash
python3 ask_jev.py "Should a one-person marketing site add a CMS?"
python3 ask_jev.py "Which fits best: Astro, Next.js, or plain HTML?" --context "one-person team, static marketing site"
python3 ask_jev.py "How risky is migrating this week?" --context-file decisions/example.json
python3 ask_jev.py "Is the copy too salesy?" "Is the copy too long?" --context-file page.txt   # one call, parallel
python3 ask_jev.py "Best channel?" --options email,linkedin,cold-call
python3 ask_jev.py "How ready is this?" --levels "Not started,Prototype,Works but rough,Polished,Shipped"
```

Jev answers three shapes: **yes/no** (`Should… / Is…`) → probability of yes; **pick one**
(`A, B, or C?`) → winner + distribution; **how much** (`How risky…`) → position on described
levels. Anything else (`Why…`, `What should the copy say…`) is rejected with a hint — Jev
does not generate text. Jev sees only what you pass in `--context` / `--context-file` / stdin.
Every call is appended to `runs/ask_log.jsonl`.

**Web UI:** double-click `Ask JEV.command` (or `python3 jev_ui.py`) — opens http://127.0.0.1:8765
with a question box, context box, live shape preview, probability bars, a "Copy for Claude"
line per answer, and your recent history. The key stays on your machine: the page talks to
`jev_ui.py`, which calls TypeSafe.

**In Claude Code:** `/jev <question>` (skill in `.claude/skills/jev`, symlinked to
`~/.claude/skills/jev` so it works in any project). Tell Claude "keep JEV in the chain of
thought for this project" and it will consult JEV at each decision point, quote the answer,
and say whether it agrees.

## Languages

Questions can be asked in **English, Chinese (simplified or traditional) and Thai**. The
parser recognises the same three shapes in each — yes/no (`…吗？`, `…ไหม`), pick one
(`A、B 还是 C`, `A หรือ B`), how much (`…有多大风险？`, `…เสี่ยงแค่ไหน`) — and the default
Score levels come back in the asker's language from [`levels.json`](levels.json). Jev is
trained primarily on English; TypeSafe says other languages, including CJK scripts, are
accepted with lower accuracy. In our checks Chinese answers tracked the English ones
closely and Thai was a little noisier, so read UNSURE and "close call" with that in mind.
Context can be in any of these languages too.

## Deploying your own public copy (Cloudflare Worker)

The Worker in `worker/index.js` serves `ui/index.html` as a static asset and proxies
`/api/ask` to TypeSafe with your key held as a secret — the browser never sees it. Per-visitor
and global rate limits are in `wrangler.toml`; per-request caps (5 questions, 6 000 chars of
context) are at the top of the Worker.

```bash
npm install
npx wrangler login
npx wrangler secret put TYPESAFE_API_KEY     # paste your key
npx wrangler deploy
```

The parser in the Worker is a port of the one in `ask_jev.py`; `npm test` checks that the two
still agree on a set of phrasings.

## jev_brainstorm.py — weigh a full decision

`jev_brainstorm.py` sends one decision spec to Jev in a single request and turns the
typed answers into a ranked recommendation. Code owns the workflow and the weights;
Jev supplies the judgments.

```bash
cp .env.example .env            # put your key from https://console.typesafe.ai/keys in it
python3 jev_brainstorm.py decisions/example.json
python3 jev_brainstorm.py decisions/example.json --weights impact=0.6,effort=0.2,risk=0.2
python3 jev_brainstorm.py decisions/example.json --replay runs/<run>.json   # re-weight, no API call
```

## Spec layout (`decisions/*.json`)

| Key | Sent to Jev as | Used for |
|---|---|---|
| `state` | request `state` (named JSON fields) | the evidence every question sees |
| `options` | one **Choice** (`best_option`) | Jev's direct pick + distribution + confidence |
| `dimensions` | one **Score** per option × dimension | composite ranking, weights adjustable in code |
| `diagnostics` | one **Noul** each | probability that a statement about the current situation is true |
| `idea_pools` | one **Choice** per pool (+ optional per-candidate effort **Score**) | ranks candidate ideas; `leverage = p / (effort + 0.15)` |

Every run is saved under `runs/` with the request and raw answers so it can be re-weighted or diffed later.
