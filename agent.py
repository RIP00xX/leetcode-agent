"""Main autonomous agent: discover unsolved problems, generate solutions with
Gemini, submit them via LeetCode's GraphQL API, and log results to SQLite.

Designed to be safely interruptible: every problem's state is persisted before
and after each step, so a Ctrl+C / crash can be resumed by re-running the
script.
"""
from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from typing import Optional

from ai_solver import GeminiSolver, QuotaExhaustedError, SolverError
from config import Settings, load_settings
from db import ACCEPTED, ATTEMPTED, Database, FAILED, SEEN
from leetcode_client import LeetCodeClient, LeetCodeError, ProblemDetail

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent")

# Graceful shutdown flag set by the SIGINT handler.
_STOP = False


def _handle_sigint(signum, frame):  # noqa: ARG001
    global _STOP
    if _STOP:
        logger.warning("Second interrupt received — forcing exit.")
        sys.exit(1)
    _STOP = True
    logger.info("Interrupt received; finishing current step then stopping.")


def _human_delay(settings: Settings, label: str = "") -> None:
    lo = min(settings.min_delay, settings.max_delay)
    hi = max(settings.min_delay, settings.max_delay)
    secs = random.uniform(lo, hi)
    if label:
        logger.debug("delay %.1fs — %s", secs, label)
    time.sleep(secs)


def _discover_problems(
    client: LeetCodeClient,
    db: Database,
    settings: Settings,
    page_size: int = 100,
    max_pages: int = 35,
) -> int:
    """Page through the full problemset and record any unseen problems."""
    discovered = 0
    for page in range(max_pages):
        if _STOP:
            break
        skip = page * page_size
        try:
            summaries = client.list_problems(limit=page_size, skip=skip)
        except LeetCodeError as exc:
            logger.error("Failed to list problems (page %d): %s", page, exc)
            raise
        if not summaries:
            break
        for s in summaries:
            db.upsert_problem(
                question_id=s.question_id,
                frontend_id=s.frontend_id,
                title=s.title,
                title_slug=s.title_slug,
                difficulty=s.difficulty,
            )
            discovered += 1
        logger.info(
            "Discovered %d problems so far (page %d, %s total in this page)",
            discovered,
            page,
            len(summaries),
        )
        if len(summaries) < page_size:
            break
        _human_delay(settings, "between problem-list pages")
    return discovered


def _solve_one(
    client: LeetCodeClient,
    solver: GeminiSolver,
    db: Database,
    settings: Settings,
    row,
) -> None:
    question_id = row["question_id"]
    title_slug = row["title_slug"]
    title = row["title"]
    frontend_id = row["frontend_id"]
    difficulty = row["difficulty"]

    logger.info(
        "=== #%s %s [%s] (qid=%s) [model: %s] ===",
        frontend_id, title, difficulty, question_id, solver.current_model,
    )

    # 1. Fetch full problem detail.
    try:
        detail: ProblemDetail = client.get_problem(title_slug)
    except LeetCodeError as exc:
        logger.error("Could not fetch problem detail: %s", exc)
        db.mark_attempted(question_id, error=str(exc))
        return

    if detail.is_paid:
        logger.info("Skipping premium (paid) problem.")
        db.mark_attempted(question_id, error="premium problem")
        return

    _human_delay(settings, "after fetching problem detail")

    # 2. Generate solution.
    try:
        code = solver.generate_solution(
            title=detail.title,
            description=detail.description_text,
            constraints=detail.constraints_text,
            starter_code=detail.starter_code,
            language=settings.leetcode_lang,
        )
    except QuotaExhaustedError:
        # Propagate to the main loop so the run stops cleanly instead of
        # marking this problem failed and churning through the next ones.
        raise
    except SolverError as exc:
        logger.error("Solution generation failed: %s", exc)
        db.mark_attempted(question_id, error=str(exc))
        return

    logger.debug("Generated code (%d chars):\n%s", len(code), code)
    _human_delay(settings, "after generating solution")

    # 3. Submit.
    try:
        submission_id = client.submit(detail.question_id, code, detail.title_slug)
    except LeetCodeError as exc:
        logger.error("Submission failed: %s", exc)
        db.mark_attempted(question_id, error=str(exc))
        return
    logger.info("Submitted (submission_id=%s); polling judge…", submission_id)

    _human_delay(settings, "after submit, before polling")

    # 4. Poll for the verdict.
    try:
        result = client.poll_submission(submission_id, timeout=90.0, interval=2.5)
    except LeetCodeError as exc:
        logger.error("Polling failed: %s", exc)
        db.mark_attempted(question_id, error=str(exc))
        return

    status = result.status_display or "Unknown"
    logger.info(
        "Result: %s | runtime=%s | memory=%s",
        status,
        result.runtime,
        result.memory,
    )

    # 5. Persist.
    db.record_result(
        question_id=question_id,
        status_display=status,
        runtime=result.runtime,
        memory=result.memory,
        submission_id=result.submission_id,
    )
    db.log_submission(
        question_id=question_id,
        title_slug=title_slug,
        submission_id=result.submission_id,
        status_display=status,
        runtime=result.runtime,
        memory=result.memory,
        lang=settings.leetcode_lang,
        code=code,
    )

    if result.pending:
        logger.warning("Judge still pending after timeout; will not retry now.")
    elif status.lower() == "accepted":
        logger.info("Accepted! Moving on.")
    else:
        logger.info("Not accepted (%s). Logged; continuing.", status)


def _print_summary(db: Database) -> None:
    summary = db.summary()
    total = sum(summary.values())
    logger.info(
        "State summary: %s (total=%d)",
        ", ".join(f"{k}={v}" for k, v in sorted(summary.items())) or "empty",
        total,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous LeetCode solver using Gemini."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-scan the full problemset for newly added problems before running.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Override MAX_PROBLEMS for this run (0 = unlimited).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    signal.signal(signal.SIGINT, _handle_sigint)

    settings = load_settings()
    logger.info(
        "Starting agent | models=%s lang=%s db=%s",
        " -> ".join(settings.gemini_models),
        settings.leetcode_lang,
        settings.db_path,
    )

    db = Database(settings.db_path)
    client = LeetCodeClient(
        settings.leetcode_session,
        lang=settings.leetcode_lang,
        csrftoken=settings.leetcode_csrftoken,
        cf_clearance=settings.cf_clearance,
        user_agent=settings.user_agent,
    )
    solver = GeminiSolver(settings.gemini_api_key, models=settings.gemini_models)

    if args.refresh or db.summary() == {}:
        logger.info("Discovering problems from LeetCode problemset…")
        try:
            n = _discover_problems(client, db, settings)
            logger.info("Discovered %d problems.", n)
        except LeetCodeError as exc:
            logger.error("Discovery failed: %s", exc)
            return 1
    else:
        logger.info("Using existing problem list from DB (pass --refresh to rescan).")

    _print_summary(db)

    limit = args.limit if args.limit is not None else settings.max_problems
    processed = 0
    consecutive_submit_fails = 0

    while not _STOP:
        remaining = db.list_unsolved(limit=1)
        if not remaining:
            logger.info("No unsolved problems remain. Done!")
            break
        if limit and processed >= limit:
            logger.info("Reached run limit of %d problems. Stopping.", limit)
            break

        # If we've had consecutive submit failures, LeetCode is throttling us.
        # Wait a few minutes before trying again instead of hammering.
        if consecutive_submit_fails >= 3:
            cooldown = 180.0  # 3 minutes
            logger.warning(
                "%d consecutive submit failures; cooling down %.0fs before retry.",
                consecutive_submit_fails,
                cooldown,
            )
            time.sleep(cooldown)
            consecutive_submit_fails = 0

        row = remaining[0]
        prev_attempts = row["attempts"] if "attempts" in row.keys() else 0
        try:
            _solve_one(client, solver, db, settings, row)
            consecutive_submit_fails = 0
        except QuotaExhaustedError as exc:
            # Daily/per-window quota is exhausted — further problems will fail
            # the same way. Stop the run cleanly so the user can re-run later
            # or switch to a paid key. Don't mark this problem as failed.
            logger.error("Gemini quota exhausted. Stopping run. %s", exc)
            break
        # Check if this problem was marked failed due to submit issues.
        updated = db.get_problem(row["question_id"])
        if updated and updated["last_error"] and "429" in str(updated["last_error"]):
            consecutive_submit_fails += 1
        elif updated and updated["last_error"] and "Cloudflare" in str(updated["last_error"]):
            consecutive_submit_fails += 1
        else:
            consecutive_submit_fails = 0
        processed += 1
        _human_delay(settings, "between problems")

    _print_summary(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
