#!/usr/bin/env python3
"""jev_brainstorm — weigh a handful of options with TypeSafe's Jev.

A decision spec (see decisions/*.json) supplies the observed state, the options,
the dimensions to score them on, yes/no diagnostics, and pools of ideas to rank.
Everything is sent to Jev in ONE request; the questions run in parallel and cannot
see each other. Code then combines the raw judgments with weights you control.

Usage:
  python3 jev_brainstorm.py decisions/trilumi_website.json
  python3 jev_brainstorm.py decisions/trilumi_website.json --weights impact=0.6,effort=0.2,risk=0.2
  python3 jev_brainstorm.py decisions/trilumi_website.json --dry-run          # print request, no call
  python3 jev_brainstorm.py decisions/trilumi_website.json --replay runs/X.json  # re-weight a saved run

Needs TYPESAFE_API_KEY in the environment or in a .env file next to this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
HERE = Path(__file__).resolve().parent
CLOSE_CALL_CONFIDENCE = 0.5  # example threshold; tune on real decisions


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
        sys.exit("TYPESAFE_API_KEY not set. Get one at https://console.typesafe.ai/keys "
                 "and export it or put TYPESAFE_API_KEY=... in JEV/.env")
    return key


def build_questions(spec: dict) -> dict:
    options = spec["options"]
    q: dict = {}

    # 1. Jev's own pick. The distribution compares the options directly.
    q["best_option"] = {
        "type": "choice",
        "instructions": spec.get("question", "Which option should be taken next, given the whole state?"),
        "criteria": options,
    }

    # 2. One Score per option per dimension (composite scoring pattern).
    for dim_id, dim in spec["dimensions"].items():
        for opt_id, opt_desc in options.items():
            q[f"{dim_id}__{opt_id}"] = {
                "type": "score",
                "instructions": {
                    "option": {"id": opt_id, "description": opt_desc},
                    "question": dim["instructions"],
                },
                "criteria": dim["levels"],
            }

    # 3. Yes/no diagnostics about the current situation.
    for diag_id, diag in spec.get("diagnostics", {}).items():
        q[f"diag__{diag_id}"] = {"type": "noul", **diag}

    # 4. Idea pools: a Choice ranks candidates; optional per-candidate effort Scores.
    for pool_id, pool in spec.get("idea_pools", {}).items():
        q[f"pool__{pool_id}"] = {
            "type": "choice",
            "instructions": pool["instructions"],
            "criteria": pool["candidates"],
        }
        if "effort_instructions" in pool:
            for cand_id, cand_desc in pool["candidates"].items():
                q[f"pool_effort__{pool_id}__{cand_id}"] = {
                    "type": "score",
                    "instructions": {
                        "candidate": {"id": cand_id, "description": cand_desc},
                        "question": pool["effort_instructions"],
                    },
                    "criteria": pool["effort_levels"],
                }
    return q


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
            sys.exit(f"TypeSafe API error {e.code}: {detail}")
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- scoring ----

def norm_score(ans: dict) -> float:
    """Score on 0..1 regardless of how many levels the question had."""
    top = len(ans["legend"]) - 1
    return ans["score"] / top if top else 0.0


def parse_weights(text: str | None, spec: dict) -> dict:
    weights = {d: v["weight"] for d, v in spec["dimensions"].items()}
    if text:
        for part in text.split(","):
            k, v = part.split("=")
            if k not in weights:
                sys.exit(f"unknown dimension {k!r}; known: {list(weights)}")
            weights[k] = float(v)
    return weights


def composite(spec: dict, answers: dict, weights: dict) -> list[dict]:
    """Higher impact is good; higher effort and risk are bad."""
    rows = []
    for opt_id in spec["options"]:
        parts = {d: norm_score(answers[f"{d}__{opt_id}"]) for d in spec["dimensions"]}
        conf = {d: answers[f"{d}__{opt_id}"]["confidence"] for d in spec["dimensions"]}
        value = sum(weights[d] * (p if d == "impact" else -p) for d, p in parts.items())
        rows.append({"option": opt_id, "value": value, "parts": parts, "confidence": conf})
    return sorted(rows, key=lambda r: r["value"], reverse=True)


def rank_pool(spec: dict, answers: dict, pool_id: str) -> list[dict]:
    pool = spec["idea_pools"][pool_id]
    probs = answers[f"pool__{pool_id}"]["probabilities"]
    rows = []
    for cand_id in pool["candidates"]:
        row = {"candidate": cand_id, "p": probs.get(cand_id, 0.0)}
        eff_key = f"pool_effort__{pool_id}__{cand_id}"
        if eff_key in answers:
            row["effort"] = norm_score(answers[eff_key])
            # value per unit of effort; the +0.15 floor stops "free" items dominating
            row["leverage"] = row["p"] / (row["effort"] + 0.15)
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("leverage", r["p"]), reverse=True)


# ----------------------------------------------------------------- report ----

def bar(x: float, width: int = 20) -> str:
    n = max(0, min(width, round(x * width)))
    return "#" * n + "." * (width - n)


def report(spec: dict, answers: dict, weights: dict, model: str) -> None:
    print(f"\n=== {spec['title']}  (model {model}) ===\n")

    pick = answers["best_option"]
    print("1) Jev's direct pick")
    for opt, p in sorted(pick["probabilities"].items(), key=lambda kv: -kv[1]):
        print(f"   {opt:<14} {bar(p)} {p:5.2f}")
    verdict = "clear" if pick["confidence"] >= CLOSE_CALL_CONFIDENCE else "CLOSE CALL - let the weights below decide"
    print(f"   -> {pick['choice']}   confidence {pick['confidence']:.2f} ({verdict})\n")

    print("2) Composite ranking   weights " + ", ".join(f"{k}={v}" for k, v in weights.items()))
    dims = list(spec["dimensions"])
    print("   " + f"{'option':<14}" + "".join(f"{d:>9}" for d in dims) + f"{'value':>9}")
    for r in composite(spec, answers, weights):
        cells = "".join(f"{r['parts'][d]:>9.2f}" for d in dims)
        low = [d for d in dims if r["confidence"][d] < CLOSE_CALL_CONFIDENCE]
        note = f"   (low confidence: {', '.join(low)})" if low else ""
        print(f"   {r['option']:<14}{cells}{r['value']:>9.2f}{note}")
    print("   (scores normalised 0-1; value = impact*w - effort*w - risk*w)\n")

    if spec.get("diagnostics"):
        print("3) Diagnostics (probability the statement is true)")
        for diag_id in spec["diagnostics"]:
            p = answers[f"diag__{diag_id}"]["noul"]
            flag = "yes" if p >= 0.7 else "no " if p <= 0.3 else "unsure"
            print(f"   {p:5.2f} {flag:<7}{diag_id}")
        print()

    for pool_id, pool in spec.get("idea_pools", {}).items():
        ans = answers[f"pool__{pool_id}"]
        print(f"4) Idea pool: {pool_id}   (top pick {ans['choice']}, confidence {ans['confidence']:.2f})")
        has_effort = "effort_instructions" in pool
        head = f"   {'candidate':<28}{'p':>6}" + (f"{'effort':>8}{'leverage':>10}" if has_effort else "")
        print(head)
        for r in rank_pool(spec, answers, pool_id):
            line = f"   {r['candidate']:<28}{r['p']:>6.2f}"
            if has_effort:
                line += f"{r['effort']:>8.2f}{r['leverage']:>10.2f}"
            print(line)
        print()


# ------------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="decision spec JSON")
    ap.add_argument("--weights", help="override dimension weights, e.g. impact=0.6,effort=0.2,risk=0.2")
    ap.add_argument("--dry-run", action="store_true", help="print the request and exit")
    ap.add_argument("--replay", help="saved run JSON to re-score instead of calling the API")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    weights = parse_weights(args.weights, spec)
    questions = build_questions(spec)

    if args.dry_run:
        print(json.dumps({"state": spec["state"], "model": MODEL, "questions": questions}, indent=2))
        print(f"\n{len(questions)} questions", file=sys.stderr)
        return

    if args.replay:
        run = json.loads(Path(args.replay).read_text())
        report(spec, run["answers"], weights, run["model"])
        return

    resp = call_jev(spec["state"], questions, load_api_key())
    runs = HERE / "runs"
    runs.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = runs / f"{stamp}-{Path(args.spec).stem}.json"
    out.write_text(json.dumps({"spec": args.spec, "model": resp["model"], "usage": resp["usage"],
                               "questions": questions, "answers": resp["answers"]}, indent=2))
    report(spec, resp["answers"], weights, resp["model"])
    print(f"usage: {resp['usage']}   saved: {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
