# LeetCode Agent

A Python agent that discovers LeetCode problems, generates solutions with Google Gemini, submits them through LeetCode's APIs, and stores results in SQLite for subsequent runs.

The project uses HTTP requests rather than browser automation. GraphQL handles problem discovery, descriptions, and judge polling; a REST endpoint handles submissions. `curl_cffi` provides Chrome TLS impersonation for the HTTP client.

## Features

- Fetches problem descriptions, constraints, and language-specific starter code.
- Selects unsolved problems by fewest attempts, then problem number, with a three-attempt limit.
- Prompts Gemini for raw code that preserves starter-code signatures.
- Supports a configurable model fallback chain when daily quotas are exhausted.
- Handles CSRF cookies, retry delays, rate limits, and Cloudflare challenge responses.
- Polls for judge verdicts, treating `Internal Error` as a transient state.
- Records verdicts, runtime, memory, submission IDs, and submitted code in SQLite.
- Supports run limits, problem-list refreshes, and graceful interruption with Ctrl+C.

## Requirements

- Python 3.10 or newer.
- A Google Gemini API key.
- Your own LeetCode account session cookie.

## Setup

```powershell
git clone <your-repository-url>
cd leetcode-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your credentials:

- `LEETCODE_SESSION`: the session cookie from your browser's DevTools under Application -> Cookies -> `https://leetcode.com`.
- `GEMINI_API_KEY`: your Gemini API key from Google AI Studio.

The client attempts to fetch a CSRF token automatically. If needed, set `LEETCODE_CSRFTOKEN` to the `csrftoken` cookie from the same browser session.

Never commit `.env` or share session cookies and API keys. The included `.gitignore` excludes credentials, local databases, logs, and virtual environments.

## Run

```powershell
# Discover problems on the first run, then start solving.
python agent.py

# Refresh the problem list before solving.
python agent.py --refresh

# Process at most five problems in this run.
python agent.py --limit 5

# Enable debug logging.
python agent.py -v
```

Press Ctrl+C to request a stop after the current step. Run the command again to continue using the stored problem state.

## Configuration

| Variable | Purpose |
| --- | --- |
| `GEMINI_MODELS` | Comma-separated model fallback chain. Configure model names available to your API key. |
| `GEMINI_MODEL` | Single-model alternative when `GEMINI_MODELS` is unset. |
| `LEETCODE_CSRFTOKEN` | Optional explicit CSRF cookie; the client sets the matching header. |
| `LEETCODE_LANG` | LeetCode language slug; defaults to `python3`. |
| `DB_PATH` | SQLite database path; defaults to `./state.db`. |
| `MIN_DELAY` / `MAX_DELAY` | Random delay between steps; defaults to 10-20 seconds. |
| `MAX_PROBLEMS` | Maximum problems processed per run; `0` means unlimited. |
| `CF_CLEARANCE` / `USER_AGENT` | Optional clearance cookie and the matching browser user agent. |

Model availability and quotas depend on your Gemini account. Configure the fallback chain for your account rather than relying on historical model names or quota figures.

## Project structure

| File | Responsibility |
| --- | --- |
| `agent.py` | CLI, discovery, solve loop, pacing, and shutdown. |
| `config.py` | Loads environment settings from `.env`. |
| `db.py` | SQLite schema, problem queue, and submission history. |
| `ai_solver.py` | Gemini prompts, retries, and model fallback. |
| `leetcode_client.py` | GraphQL reads, REST submissions, CSRF, and judge polling. |
| `.env.example` | Configuration template with placeholder credentials. |

The local reference guides informed the project and are excluded from this repository along with personal run data.

## Verification

```powershell
python -m py_compile config.py db.py leetcode_client.py ai_solver.py agent.py
python agent.py --help
```

These checks verify Python syntax and CLI imports. They do not make live submissions or establish that external APIs and configured models are currently available.

## Usage notes

Submitting generated code affects your LeetCode profile. Use a dedicated account for experiments. Session cookies expire, external APIs can change, generated solutions may fail, and premium or language-specific problems may not fit the configured pipeline.

Review the relevant service terms before running automated submissions. Keep your local SQLite database to retain run history; it is intentionally excluded from Git.
