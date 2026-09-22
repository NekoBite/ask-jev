# JEV project

TypeSafe's Jev (System One model) wrapped for Claude. Load the `typesafe:typesafe-ai` skill
before changing anything that talks to the API; the live docs at https://docs.typesafe.ai are
the source of truth for request/response shapes.

- `ask_jev.py` — natural-language question → typed Jev answer on one line. Used by the `/jev`
  skill (`.claude/skills/jev/SKILL.md`, symlinked into `~/.claude/skills/jev`).
- `jev_ui.py` + `ui/index.html` — local web UI over `ask_jev.ask()` (port 8765; `Ask JEV.command` launches it;
  `.claude/launch.json` has a `jev-ui` preview config). Vanilla HTML/CSS/JS, no build step.
- `worker/index.js` + `wrangler.toml` — public Cloudflare Worker (name `jev`, https://jev.ask-jev.workers.dev): serves `ui/` as static
  assets, proxies `/api/ask` with the `TYPESAFE_API_KEY` secret, rate-limited. The parser is a port of
  `ask_jev.py` — change both together and run `npm test` (`worker/parity.test.mjs`).
- `jev_brainstorm.py` — full decision spec → ranked recommendation. Specs live in `decisions/`.
- `runs/` — every API call is saved here (`ask_log.jsonl` for ask_jev, one JSON per brainstorm run).
- Key lives in `.env` (`TYPESAFE_API_KEY=...`); never commit it. Python is the framework 3.14
  at `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` (certs fixed there).

Jev answers only yes/no (Noul), pick-one (Choice) and how-much (Score) questions; it does not
generate text. Score levels should describe situations, not degrees. The parser handles English,
Chinese (both scripts) and Thai; default levels per language live in `levels.json`; expected
shapes per phrasing live in `worker/golden.json` (add a case there when you extend a parser).
