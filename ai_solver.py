"""Gemini-powered solution generation using the modern google-genai SDK.

Implements exponential backoff for rate-limit / transient errors and a strict
system prompt that returns only raw, executable code built on the starter.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an expert competitive programmer. Return ONLY raw, executable "
    "code for the provided LeetCode problem. Do not include markdown code "
    "blocks (e.g. ```python), explanations, or conversational text. You must "
    "build upon the provided starter code: keep the class/function signatures "
    "exactly as given, fill in the body, and add any necessary imports at the "
    "top. The code must compile and pass the sample test cases. Output nothing "
    "but the final code."
)


class SolverError(RuntimeError):
    """Recoverable generation failure (transient)."""


class QuotaExhaustedError(SolverError):
    """Gemini quota is exhausted (e.g. free-tier daily limit reached).

    Raised when the API indicates a per-day/per-window quota is used up and
    retrying shortly will not help. The agent should stop the run rather than
    churn through problems.
    """


class GeminiSolver:
    def __init__(self, api_key: str, models: tuple[str, ...] | str = "gemini-3.5-flash-lite"):
        if isinstance(models, str):
            models = (models,)
        if not models:
            raise ValueError("At least one Gemini model must be provided.")
        self._client = genai.Client(api_key=api_key)
        self._models = models
        self._model_index = 0
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            top_p=0.95,
        )

    @property
    def current_model(self) -> str:
        return self._models[self._model_index]

    def has_more_models(self) -> bool:
        return self._model_index < len(self._models) - 1

    def advance_model(self) -> bool:
        """Switch to the next model in the fallback chain. Returns False if
        there are no more models to try."""
        if not self.has_more_models():
            return False
        self._model_index += 1
        logger.info("Switched to fallback model: %s", self.current_model)
        return True

    def generate_solution(
        self,
        title: str,
        description: str,
        constraints: str,
        starter_code: Optional[str],
        language: str = "python3",
        max_retries: int = 5,
    ) -> str:
        lang_hint = _language_hint(language)
        user_prompt = self._build_prompt(
            title, description, constraints, starter_code, lang_hint
        )

        delay = 2.0
        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.current_model,
                    contents=user_prompt,
                    config=self._config,
                )
                code = (response.text or "").strip()
                if not code:
                    raise SolverError("Gemini returned an empty response.")
                return _strip_code_fences(code)
            except Exception as exc:  # noqa: BLE001 — broad for backoff
                last_err = exc
                if _is_rate_limit(exc):
                    # Use the API's own retryDelay hint when available; it
                    # reflects when the next request slot opens.
                    wait = _parse_retry_delay(exc)
                    if wait is None:
                        wait = delay * (2 ** (attempt - 1))
                    # A per-day quota won't reset in seconds — try the next
                    # fallback model instead of churning.
                    if _is_daily_quota(exc) and attempt >= 2:
                        if self.has_more_models():
                            logger.warning(
                                "Model %s daily quota exhausted; falling back "
                                "to next model.",
                                self.current_model,
                            )
                            self.advance_model()
                            return self.generate_solution(
                                title,
                                description,
                                constraints,
                                starter_code,
                                language,
                                max_retries,
                            )
                        logger.error(
                            "All Gemini models exhausted. Stopping run; "
                            "upgrade to a paid key or wait for reset."
                        )
                        raise QuotaExhaustedError(
                            "All Gemini models' per-day quotas are exhausted. "
                            "Upgrade to a paid Gemini API key or wait for the "
                            "quota to reset. Last error: %s" % exc
                        ) from exc
                    wait = min(max(wait, 1.0), 120.0)
                    logger.warning(
                        "Gemini rate-limited (attempt %d/%d); backing off %.1fs",
                        attempt,
                        max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                # Non-rate-limit errors: retry a couple times then give up.
                if attempt < max_retries:
                    logger.warning(
                        "Gemini call failed (attempt %d/%d): %s",
                        attempt,
                        max_retries,
                        exc,
                    )
                    time.sleep(min(delay * attempt, 10.0))
                    continue
                raise SolverError(
                    "Gemini generation failed after %d attempts: %s"
                    % (max_retries, exc)
                ) from exc
        raise SolverError(
            "Gemini generation failed after %d attempts: %s"
            % (max_retries, last_err)
        )

    @staticmethod
    def _build_prompt(
        title: str,
        description: str,
        constraints: str,
        starter_code: Optional[str],
        lang_hint: str,
    ) -> str:
        parts = [f"LeetCode problem: {title}", ""]
        if description:
            parts.append("Description:")
            parts.append(description)
            parts.append("")
        if constraints:
            parts.append("Constraints:")
            parts.append(constraints)
            parts.append("")
        parts.append(f"Language: {lang_hint}")
        parts.append("")
        if starter_code:
            parts.append("Starter code (build on this, keep signatures exact):")
            parts.append(starter_code)
        else:
            parts.append(
                "No starter code was provided. Write a complete, runnable "
                f"{lang_hint} solution with the standard LeetCode signature."
            )
        parts.append("")
        parts.append(
            "Return ONLY the final code. No markdown fences, no commentary."
        )
        return "\n".join(parts)


def _language_hint(lang_slug: str) -> str:
    mapping = {
        "python3": "Python 3",
        "python": "Python 3",
        "cpp": "C++",
        "c": "C",
        "java": "Java",
        "javascript": "JavaScript",
        "typescript": "TypeScript",
        "go": "Go",
        "rust": "Rust",
        "ruby": "Ruby",
        "swift": "Swift",
        "kotlin": "Kotlin",
        "scala": "Scala",
        "php": "PHP",
        "csharp": "C#",
        "sql": "MySQL",
        "mysql": "MySQL",
    }
    return mapping.get((lang_slug or "").lower(), lang_slug or "Python 3")


def _strip_code_fences(text: str) -> str:
    """Defensively remove markdown fences if the model adds them anyway."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (and optional language tag).
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        else:
            stripped = ""
        # Drop a trailing fence if present.
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped.strip()


def _is_rate_limit(exc: Exception) -> bool:
    """Heuristic: detect 429 / RESOURCE_EXHAUSTED across SDK error shapes."""
    text = str(exc).lower()
    if "429" in text or "resource_exhausted" in text or "rate" in text:
        return True
    # google-genai often exposes a .status field on client errors.
    status = getattr(exc, "status", None)
    if status is not None and "429" in str(status):
        return True
    return False


def _parse_retry_delay(exc: Exception) -> Optional[float]:
    """Extract the retryDelay (in seconds) from a RESOURCE_EXHAUSTED error.

    The google.rpc.RetryInfo detail carries a value like "26s" or "1.5s".
    Also falls back to the "Please retry in Xs" text in the message.
    """
    text = str(exc)
    # Match "retryDelay": "26s" or 'retryDelay': '26s'
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    if m:
        return float(m.group(1))
    # Match "Please retry in 26.43s" or "Please retry in 26s"
    m = re.search(r"retry in\s+(\d+(?:\.\d+)?)s", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _is_daily_quota(exc: Exception) -> bool:
    """Detect whether the exhausted quota is a per-day (hard) limit.

    Free-tier daily quotas won't reset in seconds, so retrying shortly is
    futile. We look for 'PerDay' in the quotaId or 'free_tier' in the metric.
    """
    text = str(exc)
    return "perday" in text.lower() or "free_tier" in text.lower()
