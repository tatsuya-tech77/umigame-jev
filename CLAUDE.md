# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Browser game: the host of a "ウミガメのスープ" (lateral thinking puzzle) is played by TypeSafe AI's **Jev**, a decision-only model that returns typed probabilities (Choice / Score / Noul) and never generates text. All player-facing text (stories, truths, hints, canned replies) is hand-written; Jev only classifies. UI and docs are in Japanese.

The owner plans to publish a Qiita article and promote on SNS; drafts go in `docs/`.

## Commands

```bash
.venv/bin/python -m umigame.web                    # serve on http://127.0.0.1:8789/
JEV_MOCK=1 .venv/bin/python -m umigame.web         # no API calls (random answers, UI check only)
.venv/bin/python -m unittest discover -s tests     # logic tests, no API
.venv/bin/python -m unittest tests.test_judge.GuessTest.test_all_keys_is_correct   # single test
.venv/bin/python -m umigame.evaluate               # accuracy/cost/latency against the real API
```

`.venv` may not exist yet; `../jev-lab/.venv` has the same dependencies. The API key lives in `.env` (`TYPESAFE_API_KEY`), loaded by `umigame/client.py` without overriding existing env vars.

Deploy: Google Cloud Run in asia-northeast1 with `--max-instances 1`, the budget file on a Cloud Storage volume at `/data`, the Jev key in Secret Manager (`Dockerfile`, `.gcloudignore`, steps in `docs/deploy.md`). Keep it at one instance: rate limit and answer cache live in process memory. The image includes only `results/arena-claude-code.jsonl` from `results/`.

Do not stop or restart servers on ports 8787 / 8788 (the owner's other apps). Kill only by port, e.g. `lsof -ti tcp:8789 -sTCP:LISTEN | xargs -r kill`.

## Architecture

The full design doc (Japanese, human-facing, also the basis of the Qiita article) is `docs/architecture.md`. Keep its numbers in sync when changing constants, rules or APIs.
Before changing `decide()`, thresholds or the questions asked in `answer()`, read `docs/research-decide.md` (evaluation of alternatives) and re-run `python -m tools.eval_decide collect` / `compare` against `results/eval/hard_set.json`.

The core rule: **Jev classifies, `judge.py` decides.** Thresholds, answer text and win conditions are code, not model output.

- `umigame/puzzles.py` — puzzle data. Per puzzle: `story` (shown), `truth` + `facts` (sent to Jev only), `keys` (points needed to solve; drive the meter, hints and guess judging), `hints` (hand-written `[weak, strong]` per key, same order/length as `keys`), `decoys` (plausible wrong hypotheses), and evaluation data `gold` / `guesses`. `META_INPUTS` are non-question inputs for evaluation.
- `umigame/judge.py` — every Jev call and all game logic.
  - `answer()`: one request asks five questions — the yes/no/irrelevant Choice, an input-kind Choice (question / hint request or instruction / compound / other), `CONTRADICTS`, `ALLTRUE` and `INJECTION` Nouls (instructions to the host mixed into the input; ≥ 0.5 → canned reply, because such inputs could flip borderline answers). The last `HISTORY_TURNS` Q&As are included because Jev is stateless and questions use pronouns ("その人は…"); when history exists, `CONTRADICTS` is also asked in a parallel second request without history (history biases Jev toward yes). `decide()` maps probabilities to a label, biased to avoid yes⇄no flips (yes ≥ 0.5, not-yes ≤ 0.3, otherwise "unsure"); a yes becomes "partly" if contradicts ≥ 0.5, alltrue < 0.5 or history-free contradicts ≥ 0.4 (Jev tends to say yes to half-right questions).
  - `progress()`: one Noul per key over the full Q&A history. `Progress.band` → 遠い／近い／あと少し／そろった (そろった = every key ≥ `ALL_FOUND` = 0.7). Shown only for ★3+, every 3 counted questions, identical for humans and AIs.
  - `judge_guess()`: Nouls for keys **and** decoys in one request, without the truth text. ≥2 decoys, or a decoy plus ≥2 keys → `scattershot`; any decoy or no key → `wrong`; all keys → `correct`; some → `close` (shown with `key_labels` as ✓/✗).
  - `pick_hint()`: chooses the least-reached key's next hint from `progress()` output.
  - `cache_key()`: identical questions get the first answer (Jev probabilities jitter ±0.05); questions with demonstratives are not cached.
- `umigame/web.py` — FastAPI, stateless for game progress (the browser sends history every call). Server-side: answer cache and per-IP rate limit (in memory, sliding 60 s window, `UMIGAME_PER_IP_PER_MIN`; behind a load balancer set `UMIGAME_TRUST_PROXY_HOPS` so `client_ip()` reads `X-Forwarded-For` from the right), and a daily spending cap in yen (`Guard`, `UMIGAME_DAILY_BUDGET_YEN`, JST days, persisted to `.budget.json`). When the cap is hit the shop "closes" for the day: API returns 503 with `{"code": "closed"}` and the UI disables input. Never return `truth` except on a correct guess or `/api/reveal`; never return key text or per-key guess probabilities.
- `umigame/session.py` — rules shared by the CLI and the OpenRouter arena (20 counted questions, 10 uncounted inputs, 3 guesses, score = questions + 2 × wrong guesses, meter), plus the file-backed `AnswerCache`. The web UI reimplements the same rules in JS; keep them in sync.
- `umigame/play_cli.py` — play one move per command; used to run Claude Opus as the AI rival via Claude Code subagents (one fresh subagent per run). Finished games append to `results/arena-claude-code.jsonl`; `web.py` shows the median per puzzle as the rival's score. After changing a puzzle's keys/facts, purge its entries from `results/answer_cache.json` and rerun.
- `tools/codex_play.py` — the same games with ChatGPT models via the Codex CLI bundled in ChatGPT.app (`codex exec` in an empty temp dir with a `./play` wrapper; any other command disqualifies the run and removes its record).
- `umigame/arena.py` — the same games against OpenRouter models (needs `OPENROUTER_API_KEY`, `--max-usd` budget; currently unused).
- `umigame/static/index.html` — single-file frontend. Game state is kept in `localStorage` (`umigame-v3`); history entries are `{q, a}` with answer labels (`yes/no/irrelevant/partly/unsure`). Render server strings with `textContent`. Puzzle images are `static/img/<id>.png`, 4-tone pixel art made with `tools/palette.py` (see `docs/image-prompts.md`).
- `umigame/evaluate.py` — runs `gold`, `META_INPUTS` and `guesses` against the real API. Numbers are optimistic because thresholds were tuned on the same data.

## Jev constraints that shape the design

Text in, typed judgments out; 64k context; about $0.042 / 1M input tokens, output free; 1,200 requests/min per key; weak at multi-hop reasoning, counting and dates; accuracy drops with irrelevant state; vulnerable to prompt injection. Put more questions in the same request rather than making extra requests. Question IDs (dict keys) are not seen by the model; instructions and criteria text are.
