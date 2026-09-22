#!/usr/bin/env python3
"""ask_jev — ask TypeSafe's Jev a plain-English question, get a simple typed answer.

Jev does not write prose. It answers three shapes of question, so this script turns
a natural sentence into one of them and turns the typed answer back into one line:

  yes/no      "Should we add a CMS?"                         -> Noul   (probability of yes)
  pick one    "Astro, Next.js, or plain HTML?"                -> Choice (winner + distribution)
  how much    "How risky is migrating this week?"             -> Score  (position on described levels)

Usage:
  python3 ask_jev.py "Should Trilumi add a CMS?"
  python3 ask_jev.py "Which fits best: Astro, Next.js, or plain HTML?" --context "one-person team, static marketing site"
  python3 ask_jev.py "How risky is migrating this week?" --context-file notes.md
  python3 ask_jev.py "Is the copy too salesy?" "Is the copy too long?" --context-file page.txt   # several questions, one call
  python3 ask_jev.py "Best channel?" --options email,linkedin,cold-call
  python3 ask_jev.py "How ready is this for launch?" --levels "Not started,Prototype,Works but rough,Polished,Shipped"
  python3 ask_jev.py "..." --json          # full answer for programmatic use
  python3 ask_jev.py "..." --dry-run       # show the typed question, no API call

Context is everything Jev knows: it sees --context / --context-file / stdin, nothing else.
Every call is appended to runs/ask_log.jsonl.
Needs TYPESAFE_API_KEY in the environment or in a .env file next to this script.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
HERE = Path(__file__).resolve().parent
LOG = HERE / "runs" / "ask_log.jsonl"

YES_ABOVE, NO_BELOW = 0.7, 0.3       # example thresholds for reading a Noul; tune per decision
CLOSE_CALL_CONFIDENCE = 0.5


class JevError(Exception):
    """Anything the caller should show to the user instead of a traceback."""


# ---------------------------------------------------------------- request ----

def load_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        env = HERE / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise JevError("TYPESAFE_API_KEY not set. Get one at https://console.typesafe.ai/keys "
                       "and export it or put TYPESAFE_API_KEY=... in JEV/.env")
    return key


def call_jev(state, questions: dict, api_key: str, retries: int = 4) -> dict:
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in (429, 529) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise JevError(f"TypeSafe API error {e.code}: {detail}")
    raise RuntimeError("unreachable")


# -------------------------------------------------- natural language -> typed ----

AUX = r"(?:is|are|am|was|were|do|does|did|should|shall|can|could|will|would|has|have|had|must|may|might|need|needs|ought)"
YESNO_RE = re.compile(rf"^\s*{AUX}\b", re.I)
DEGREE_RE = re.compile(r"^\s*how\s+(\w+)", re.I)
WHICH_RE = re.compile(r"^\s*(?:which|what)\b", re.I)
OR_RE = re.compile(r"\s+or\s+", re.I)

# Score levels describe situations, not degrees (see docs: "Writing good levels").
# Keyed by the adjective after "how ..."; the last entry is the fallback.
LEVEL_SETS: list[tuple[tuple[str, ...], list[str]]] = [
    (("likely", "probable", "plausible", "realistic"), [
        "Almost certainly will not happen",
        "Unlikely; would need something unusual to go right",
        "Could go either way",
        "Likely; the normal outcome unless something goes wrong",
        "Almost certain to happen",
    ]),
    (("risky", "dangerous", "safe", "unsafe"), [
        "Nothing meaningful can go wrong",
        "Small, easily reversible problems are possible",
        "Real chance of a setback that costs time or money to undo",
        "Serious harm is likely unless it is actively mitigated",
        "Likely to cause severe or irreversible damage",
    ]),
    (("hard", "difficult", "complex", "complicated", "easy", "simple", "big", "large"), [
        "Trivial; minutes of routine work",
        "Straightforward; a known recipe, under a day",
        "Moderate; several days with some unknowns",
        "Hard; weeks of work, needs expertise, real unknowns",
        "Very hard; months of work or may not be achievable",
    ]),
    (("urgent", "important", "critical", "pressing"), [
        "Can wait indefinitely at no cost",
        "Nice to have soon; waiting weeks costs nothing",
        "Should be done within days; waiting has a modest cost",
        "Needed today; delay causes real harm",
        "Critical right now; every hour of delay causes damage",
    ]),
    (("good", "strong", "well", "ready", "mature", "solid", "polished", "healthy", "effective", "clear", "bad", "weak"), [
        "Poor; fails at its basic purpose",
        "Weak; works but with major gaps",
        "Adequate; does the job with noticeable rough edges",
        "Good; solid with only minor gaps",
        "Excellent; hard to improve",
    ]),
    (("valuable", "useful", "worthwhile", "profitable", "much", "many"), [
        "No value; nothing gained",
        "Marginal value; barely worth the effort",
        "Useful; a clear but modest gain",
        "High value; a significant gain",
        "Transformative; changes the outcome entirely",
    ]),
    ((), [  # fallback for any other "how <adjective>"
        "Not at all",
        "A little",
        "A moderate amount",
        "A lot",
        "An extreme amount",
    ]),
]


def levels_for(adjective: str) -> list[str]:
    adj = adjective.lower()
    for words, levels in LEVEL_SETS:
        if adj in words:
            return levels
    return LEVEL_SETS[-1][1]


def split_options(question: str) -> list[str]:
    """Pull 'A, B, or C' style options out of a sentence. Empty list if none found."""
    body = question.strip().rstrip("?.! ").strip()
    if not OR_RE.search(body) and "," not in body:
        return []

    if ":" in body:
        seg = body.split(":", 1)[1]
    elif m := re.search(r"\b(?:between|among|out of)\s+(.+)$", body, re.I):
        seg = m.group(1)
    elif WHICH_RE.match(body) and "," in body:
        seg = body.split(",", 1)[1]
    else:
        # "Should we redesign or leave it?" -> "redesign or leave it"
        seg = re.sub(rf"^\s*{AUX}\s+(?:we|i|you|they|it|one|the team)\s+(?:(?:be\s+)?(?:better|best|wiser|smarter|safer)\s+)?(?:to\s+)?",
                     "", body, flags=re.I)
        if WHICH_RE.match(seg):
            # "Which is faster to build with Astro or Next.js" -> text after the last lead verb before the first "or"
            first_or = OR_RE.search(seg)
            if not first_or:
                return []
            prefix = seg[:first_or.start()]
            leads = list(re.finditer(r"\b(?:is|are|be|to|use|using|choose|pick|prefer|with|take|go|do|for)\s+", prefix, re.I))
            if not leads:
                return []
            seg = seg[leads[-1].end():]

    parts = re.split(r"\s*,\s*(?:or|and)\s+|\s+or\s+|\s*,\s*", seg.strip(), flags=re.I)
    opts = [p.strip(" \"'") for p in parts]
    opts = [re.sub(r"^(?:rather|instead|just|simply)\s+", "", o, flags=re.I) for o in opts if o]
    if not 2 <= len(opts) <= 20 or any(len(o) > 80 for o in opts):
        return []
    return opts


def typed_question(question: str, force: str | None, options: list[str] | None,
                   levels: list[str] | None) -> tuple[dict, str]:
    """Return (question object for the API, note about how it was interpreted)."""
    q = question.strip()
    if options:
        return {"type": "choice", "instructions": q, "criteria": {o: None for o in options}}, "choice (options given)"
    if levels:
        return {"type": "score", "instructions": q, "criteria": levels}, "score (levels given)"

    kind = force
    if kind is None:
        if m := DEGREE_RE.match(q):
            kind = "score"
            levels = levels_for(m.group(1))
        elif YESNO_RE.match(q) and not (WHICH_RE.match(q)):
            found = split_options(q)
            kind = "choice" if found else "noul"
            options = found
        elif WHICH_RE.match(q):
            options = split_options(q)
            kind = "choice" if options else None
        else:
            kind = None

    if kind == "noul":
        return {"type": "noul", "instructions": q}, "yes/no"
    if kind == "choice":
        options = options or split_options(q)
        if not options:
            raise JevError("Could not find the options in that question. List them with --options a,b,c "
                           "or phrase it like 'X, Y, or Z?'")
        return {"type": "choice", "instructions": q, "criteria": {o: None for o in options}}, "pick one"
    if kind == "score":
        levels = levels or levels_for((DEGREE_RE.match(q) or [None, "much"])[1])
        return {"type": "score", "instructions": q, "criteria": levels}, "how much (default levels; pass --levels for sharper ones)"

    raise JevError("Jev answers yes/no, pick-one, or how-much questions only; it does not write explanations.\n"
                   "Rephrase, e.g.  'Is X true?'  |  'A, B, or C?'  |  'How risky is X?'\n"
                   "or force a shape with --type noul|choice|score plus --options / --levels.")


# ----------------------------------------------------------------- answers ----

def read_answer(question: str, ans: dict) -> tuple[str, str]:
    """One-line verdict plus an optional detail line."""
    if ans["type"] == "noul":
        p = ans["noul"]
        verdict = "YES" if p >= YES_ABOVE else "NO" if p <= NO_BELOW else "UNSURE"
        return f"{verdict} (p(yes)={p:.2f})", ""
    if ans["type"] == "choice":
        conf = ans["confidence"]
        tag = "" if conf >= CLOSE_CALL_CONFIDENCE else ", close call"
        dist = " | ".join(f"{k} {v:.2f}" for k, v in sorted(ans["probabilities"].items(), key=lambda kv: -kv[1]))
        return f"{ans['choice']} (p={ans['probabilities'][ans['choice']]:.2f}, confidence {conf:.2f}{tag})", f"options: {dist}"
    if ans["type"] == "score":
        top = len(ans["legend"]) - 1
        nearest = ans["legend"][str(round(ans["score"]))]
        conf = ans["confidence"]
        tag = "" if conf >= CLOSE_CALL_CONFIDENCE else ", close call"
        dist = " | ".join(f"[{i}] {ans['legend'][i][:38]} {ans['probabilities'][i]:.2f}" for i in ans["legend"])
        return f"{ans['score']:.2f} on 0-{top} = \"{nearest}\" (confidence {conf:.2f}{tag})", f"levels: {dist}"
    return json.dumps(ans), ""


def build_state(context: str | None, context_file: str | None, use_stdin: bool) -> object:
    parts: dict = {}
    if context_file:
        path = Path(context_file)
        text = path.read_text()
        if path.suffix == ".json":
            data = json.loads(text)
            parts["context"] = data.get("state", data) if isinstance(data, dict) else data
        else:
            parts["context"] = text
    if use_stdin:
        parts["stdin"] = sys.stdin.read()
    if context:
        parts["notes"] = context
    if not parts:
        return {"context": "No specific context was supplied. Judge from the question alone and general knowledge."}
    return parts


def log_run(record: dict) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------- ask ----

def ask(questions_text: list[str], state, force: str | None = None, options: list[str] | None = None,
        levels: list[str] | None = None, dry_run: bool = False) -> dict:
    """Turn plain questions into typed ones, call Jev, read and log the answers.

    Raises JevError for anything the caller should show the user.
    """
    if levels and not 2 <= len(levels) <= 10:
        raise JevError("--levels needs between 2 and 10 entries")
    questions, notes = {}, {}
    for i, q in enumerate(questions_text):
        qid = f"q{i + 1}"
        questions[qid], notes[qid] = typed_question(q, force, options, levels)
    if dry_run:
        return {"request": {"state": state, "model": MODEL, "questions": questions},
                "shapes": [notes[f"q{i + 1}"] for i in range(len(questions_text))]}

    resp = call_jev(state, questions, load_api_key())
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for i, q in enumerate(questions_text):
        qid = f"q{i + 1}"
        ans = resp["answers"][qid]
        verdict, detail = read_answer(q, ans)
        results.append({"question": q, "shape": notes[qid], "verdict": verdict, "detail": detail,
                        "typed_question": questions[qid], "answer": ans})
        log_run({"ts": stamp, "model": resp["model"], "question": q, "shape": notes[qid],
                 "state": state, "typed_question": questions[qid], "answer": ans, "verdict": verdict})
    return {"model": resp["model"], "usage": resp["usage"], "state": state, "results": results}


# ------------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="+", help="one or more plain-English questions (all share the same context)")
    ap.add_argument("--context", "-c", help="facts Jev should consider (it sees nothing else)")
    ap.add_argument("--context-file", "-f", help="text or JSON file used as context (JSON: its 'state' key if present)")
    ap.add_argument("--stdin", action="store_true", help="read extra context from stdin")
    ap.add_argument("--options", "-o", help="comma-separated options, forces a pick-one question")
    ap.add_argument("--levels", "-l", help="comma-separated ordered levels (2-10), forces a how-much question")
    ap.add_argument("--type", "-t", choices=["noul", "choice", "score"], help="force the question shape")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="also print the typed question sent to Jev")
    ap.add_argument("--dry-run", action="store_true", help="print the request and exit without calling the API")
    args = ap.parse_args()

    options = [o.strip() for o in args.options.split(",")] if args.options else None
    levels = [l.strip() for l in args.levels.split(",")] if args.levels else None
    state = build_state(args.context, args.context_file, args.stdin)

    try:
        if args.dry_run:
            print(json.dumps(ask(args.question, state, args.type, options, levels, dry_run=True)["request"], indent=2))
            return
        if args.verbose:
            typed = ask(args.question, state, args.type, options, levels, dry_run=True)["request"]["questions"]
            print("typed question(s) sent to Jev:", file=sys.stderr)
            print(json.dumps(typed, indent=2), file=sys.stderr)
        out = ask(args.question, state, args.type, options, levels)
    except JevError as e:
        sys.exit(str(e))

    if args.json:
        print(json.dumps(out, indent=2))
        return
    for r in out["results"]:
        print(f"JEV: {r['verdict']}   <- \"{r['question']}\"  [{r['shape']}]")
        if r["detail"]:
            print(f"     {r['detail']}")
    sys.stdout.flush()
    print(f"     (model {out['model']}, {out['usage']['input_tokens']} in / {out['usage']['output_tokens']} out, "
          f"logged to runs/ask_log.jsonl)", file=sys.stderr)


if __name__ == "__main__":
    main()
