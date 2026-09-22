---
name: jev
description: Ask JEV (TypeSafe's System One model) a plain-English question — yes/no, pick-one, or how-much — and feed its calibrated typed answer straight into Claude's reasoning. Use when the user types /jev, says "ask JEV", or has asked to keep JEV "in the chain of thought" / "in the loop" for a project — then consult JEV at every real decision point without being asked.
allowed-tools: Bash(python3 /Users/nekobiteandthem1/Documents/Claude/JEV/ask_jev.py:*)
---

# /jev — JEV in the chain of thought

JEV is not a chat model. It returns calibrated probabilities over answers you define,
in ~1 s, with no explanation. Treat it as a second opinion from a well-calibrated
judge that has read ONLY the context you hand it.

## How to ask

Run the CLI with Bash and read its stdout — that is the answer, feed it into your reasoning:

```bash
python3 /Users/nekobiteandthem1/Documents/Claude/JEV/ask_jev.py "<question>" --context "<the facts that matter>"
```

- `$ARGUMENTS` after `/jev` is the question. If the user gave no context, write a `--context`
  string yourself from what you know of the task (JEV cannot see this conversation).
- Three shapes only — phrase the question as one of them:
  - yes/no: `"Should we add a CMS?"`, `"Is this copy too salesy?"`
  - pick one: `"Which fits best: Astro, Next.js, or plain HTML?"` (or pass `--options a,b,c`)
  - how much: `"How risky is migrating this week?"` (default situational levels; pass
    `--levels "l0,l1,...,l4"` when you can describe the levels concretely — sharper answers)
- Several independent questions about the same context go in ONE call (all positional
  args); they run in parallel.
- `--context-file path` accepts text or JSON (a decision spec's `state` key is used if present).
- `--json` for the raw distribution; `--dry-run` to see the typed question without spending a call.

## How to read the answer

```
JEV: YES (p(yes)=0.86)                                <- "..."  [yes/no]
JEV: Next.js (p=0.71, confidence 0.62)                <- "..."  [pick one]
     options: Next.js 0.71 | Astro 0.22 | plain HTML 0.07
JEV: 2.34 on 0-4 = "Real chance of a setback ..." (confidence 0.70)  <- "..."  [how much]
```

- yes/no: ≥0.70 YES, ≤0.30 NO, between = UNSURE. UNSURE is information: the case is
  genuinely borderline on the evidence given — say so, or add context and re-ask.
- "close call" (confidence < 0.5) on pick-one/how-much means probability is spread; look at
  the distribution before leaning on the winner.
- JEV only knows what was in `--context`. If the answer surprises you, first check whether
  the context was missing a fact, then re-ask with it. Do not re-ask the identical question
  hoping for a different number.

## Chain-of-thought protocol (when the user asked for JEV in the loop on a project)

At each real decision point — choosing between approaches, judging a risk, checking
whether something is good enough to ship, deciding whether a claim holds:

1. State the decision and the facts in one or two lines.
2. Ask JEV (one call, several questions if they are independent).
3. Quote JEV's line verbatim in your reply, then say what you will do and whether you agree.
   If you overrule JEV, say why in one sentence.
4. Proceed. Do not ask JEV about trivia, things code can check exactly, or open-ended
   "what should the copy say" questions — it cannot generate.

Every call is logged to `JEV/runs/ask_log.jsonl`; at the end of the project, summarise where
JEV agreed, disagreed, and was UNSURE, so the user can judge whether it earned its place.
