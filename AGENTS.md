# leetcode-agent

Autonomous agent that solves LeetCode problems using Google Gemini. It uses
LeetCode's own APIs (the same ones the website uses) with your own session
cookie — no browser automation. HTTP uses `curl_cffi` with Chrome TLS
impersonation because plain `requests` gets flagged by Cloudflare's JA3
fingerprinting on the submit endpoint.

## How it works

1. **Discover**: pages through the problemset list and records every problem in
   a local SQLite database (`state.db`).
2. **Pick**: selects the least-attempted unsolved problem (`attempts < 3`),
   then lowest problem number.
3. **Fetch**: pulls the full description, constraints, and starter code for that
   problem via the `question` GraphQL query (constraints are extracted from the
   `content` HTML — there is no `constraints` field).
4. **Generate**: asks Gemini to produce a solution that builds on the starter
   code, with a strict "code only, no markdown" system prompt. Uses the
   `GEMINI_MODELS` fallback chain — on daily-quota errors it retries the same
   prompt on the next model; when all models are exhausted it raises
   `QuotaExhaustedError` and stops cleanly.
5. **Submit**: POSTs to the REST endpoint `/problems/<slug>/submit/` (the old
   `submitSolution` GraphQL mutation no longer exists). Needs `csrftoken`
   cookie + matching `x-csrftoken` header. Retries 429/Cloudflare-403 with a
   `[30,60,120,240]s` backoff.
6. **Poll**: waits for the judge verdict via the `submissionDetails` GraphQL
   query. Treats `Internal Error` as a transient state and keeps polling.
7. **Log**: persists status, runtime, memory, and the submitted code to SQLite
   so the run resumes cleanly after any interruption.

## Setup

```powershell
cd D:\leetcode-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in:

- `LEETCODE_SESSION` — from your browser DevTools → Application → Cookies →
  `https://leetcode.com` → `LEETCODE_SESSION`. The `csrftoken` is fetched
  automatically at runtime.
- `GEMINI_API_KEY` — from https://aistudio.google.com/apikey.

Optional overrides: `GEMINI_MODELS` (comma-separated fallback chain;
`GEMINI_MODEL` single name still works), `LEETCODE_CSRFTOKEN`,
`LEETCODE_LANG` (default `python3`), `DB_PATH`, `MIN_DELAY`/`MAX_DELAY`
(default 10–20s), `MAX_PROBLEMS`, `CF_CLEARANCE` + `USER_AGENT`
(Cloudflare clearance cookie harvested from a real browser — must be paired
with the matching UA).

## Running

```powershell
# First run discovers the problemset, then starts solving.
python agent.py

# Re-scan for newly added problems before running.
python agent.py --refresh

# Solve at most 5 problems this run.
python agent.py --limit 5

# Debug logging.
python agent.py -v
```

The agent is interruptible: Ctrl+C finishes the current step and stops. Re-run
to resume from where it left off.

## Files

- `config.py` — loads settings from `.env`.
- `db.py` — SQLite schema and helpers (problem state + submission log).
- `leetcode_client.py` — `curl_cffi`-based client (GraphQL list/detail/poll,
  REST submit, CSRF, Cloudflare detection).
- `ai_solver.py` — Gemini solution generation with a multi-model fallback
  chain and backoff.
- `agent.py` — main loop.

## Notes

- This uses your own LeetCode account. Submitting AI-generated solutions
  affects your profile stats — use a dedicated account.
- If you see HTTP 401/403, the `LEETCODE_SESSION` cookie has expired; refresh
  it — but check the body first: a 403 containing "Just a moment" HTML is a
  Cloudflare challenge (transient / TLS-fingerprint), not expired auth.
- LeetCode enforces a per-account submission cap (~1,000/day); on sustained
  429s just re-run later — state is preserved.
- Never print or log secrets (`.env` values); previously exposed credentials
  must be rotated.

## Verification

```powershell
python -m py_compile config.py db.py leetcode_client.py ai_solver.py agent.py
```
