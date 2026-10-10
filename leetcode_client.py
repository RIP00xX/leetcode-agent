"""Thin, well-typed wrapper around LeetCode's public GraphQL endpoint.

All network access for the agent goes through here. Authentication uses the
``LEETCODE_SESSION`` cookie; the ``csrftoken`` cookie (required for mutations)
is fetched automatically on the first request to leetcode.com.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import Any, Optional

try:
    # curl_cffi impersonates a real browser's TLS fingerprint (JA3/JA4),
    # which is what Cloudflare's bot detection actually inspects. Plain
    # `requests` gets permanently challenged; curl_cffi passes as Chrome.
    from curl_cffi import requests
    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    import requests  # type: ignore[no-redef]
    _HAS_CURL_CFFI = False

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://leetcode.com/graphql"
BASE_URL = "https://leetcode.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- GraphQL operations -----------------------------------------------------

_PROBLEMSET_QUERY = """
query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList: questionList(
    categorySlug: $categorySlug
    limit: $limit
    skip: $skip
    filters: $filters
  ) {
    questions: data {
      questionId
      questionFrontendId
      title
      titleSlug
      difficulty
      topicTags { name }
      status
    }
    totalNum
  }
}
"""

_QUESTION_QUERY = """
query questionData($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
    questionFrontendId
    title
    titleSlug
    content
    difficulty
    isPaidOnly
    codeSnippets { lang langSlug code }
    sampleTestCase
    exampleTestcases
  }
}
"""

# Note: LeetCode has removed the `submitSolution` GraphQL mutation (the
# `QuestionSolutionSubmission` type no longer exists in their schema, and
# introspection is disabled). Submissions go through the legacy REST endpoint
# POST /problems/<slug>/submit/ instead, which returns {"submission_id": ...}.
# Polling the result is still done via the submissionDetails GraphQL query.

_SUBMISSION_DETAILS_QUERY = """
query submissionDetails($submissionId: Int!) {
  submissionDetails(submissionId: $submissionId) {
    id
    statusCode
    statusDisplay
    runtime
    memory
    lang { name verboseName }
    code
    totalTestcases
    runtimePercentile
    memoryPercentile
  }
}
"""


# --- data containers --------------------------------------------------------


@dataclass
class ProblemSummary:
    question_id: str
    frontend_id: str
    title: str
    title_slug: str
    difficulty: str
    status: Optional[str]  # "ac" if accepted, else None/""


@dataclass
class ProblemDetail:
    question_id: str
    frontend_id: str
    title: str
    title_slug: str
    difficulty: str
    is_paid: bool
    description_text: str  # HTML stripped to plain text
    constraints_text: str
    starter_code: Optional[str]  # snippet for the configured language
    sample_test_case: str


@dataclass
class SubmissionResult:
    submission_id: Optional[str]
    status_display: str
    runtime: Optional[str]
    memory: Optional[str]
    pending: bool


class LeetCodeError(RuntimeError):
    """Raised for unrecoverable LeetCode API failures (auth, schema, etc)."""


# --- client -----------------------------------------------------------------


class LeetCodeClient:
    def __init__(
        self,
        session_cookie: str,
        lang: str = "python3",
        csrftoken: Optional[str] = None,
        cf_clearance: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self._lang = lang
        ua = user_agent or USER_AGENT
        if _HAS_CURL_CFFI:
            # Impersonate Chrome's TLS/HTTP2 fingerprint so Cloudflare sees a
            # real browser instead of Python's OpenSSL signature.
            self._session = requests.Session(impersonate="chrome")
        else:
            self._session = requests.Session()
            logger.warning(
                "curl_cffi not installed — using plain requests. Cloudflare "
                "bot challenges may block submissions. Run: pip install curl_cffi"
            )
        self._session.headers.update(
            {
                "User-Agent": ua,
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self._session.cookies.set("LEETCODE_SESSION", session_cookie)
        if cf_clearance:
            # Cloudflare clearance cookie obtained by solving the JS challenge
            # in a real browser. Bound to your IP + User-Agent — the UA above
            # must match the browser you took it from.
            self._session.cookies.set("cf_clearance", cf_clearance)
        self._csrf_token: Optional[str] = None
        if csrftoken:
            # Use the user-provided csrftoken directly — this is the reliable
            # path, since the token must match what LeetCode issued for this
            # session. Set both the cookie and the header.
            self._csrf_token = csrftoken
            self._session.cookies.set("csrftoken", csrftoken)
            self._session.headers["x-csrftoken"] = csrftoken

    # ---- low-level -------------------------------------------------------

    def _ensure_csrf(self) -> None:
        if self._csrf_token:
            return
        logger.debug("Fetching csrftoken from leetcode.com homepage")
        resp = self._session.get(BASE_URL + "/", timeout=30)
        if resp.status_code in (401, 403):
            raise LeetCodeError(
                "LeetCode rejected the session cookie (HTTP %d). The "
                "LEETCODE_SESSION cookie is likely expired — refresh it from "
                "your browser." % resp.status_code
            )
        resp.raise_for_status()
        token = resp.cookies.get("csrftoken")
        if not token:
            # Some accounts set csrftoken only on the problems page; try there.
            resp = self._session.get(BASE_URL + "/problemset/all/", timeout=30)
            token = resp.cookies.get("csrftoken")
        if not token:
            raise LeetCodeError(
                "Could not obtain a csrftoken cookie. The LEETCODE_SESSION "
                "cookie may be invalid or expired."
            )
        self._csrf_token = token
        self._session.headers["x-csrftoken"] = token

    def _gql(
        self,
        query: str,
        variables: dict[str, Any],
        operation_name: Optional[str] = None,
        is_mutation: bool = False,
        retries: int = 3,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict:
        if is_mutation:
            self._ensure_csrf()
        payload: dict[str, Any] = {"query": query, "variables": variables}
        if operation_name:
            payload["operationName"] = operation_name

        headers = extra_headers or {}

        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                resp = self._session.post(
                    GRAPHQL_URL, json=payload, headers=headers, timeout=30
                )
            except requests.exceptions.RequestException as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
                continue

            if resp.status_code in (401, 403):
                body = resp.text[:500] if resp.text else "(empty body)"
                raise LeetCodeError(
                    "LeetCode rejected the request (HTTP %d). The "
                    "LEETCODE_SESSION cookie is likely expired, or the "
                    "csrftoken is missing/invalid for mutations. Response "
                    "body: %s" % (resp.status_code, body)
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2.0 * (attempt + 1)
                logger.warning(
                    "LeetCode returned HTTP %d; backing off %.1fs",
                    resp.status_code,
                    wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise LeetCodeError(
                    "LeetCode request failed: HTTP %d %s"
                    % (resp.status_code, resp.text[:300])
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise LeetCodeError("LeetCode returned non-JSON: %s" % resp.text[:300])
            if data.get("errors"):
                msgs = "; ".join(
                    e.get("message", str(e)) for e in data["errors"]
                )
                # Some transient errors are retriable.
                if attempt < retries - 1 and "rate" in msgs.lower():
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise LeetCodeError("LeetCode GraphQL errors: %s" % msgs)
            return data["data"]
        raise LeetCodeError(
            "LeetCode request failed after %d retries: %s" % (retries, last_err)
        )

    # ---- high-level ------------------------------------------------------

    def list_problems(
        self, limit: int = 100, skip: int = 0
    ) -> list[ProblemSummary]:
        """Fetch one page of the problemset. Returns summaries in order."""
        data = self._gql(
            _PROBLEMSET_QUERY,
            {"categorySlug": "", "limit": limit, "skip": skip, "filters": {}},
            operation_name="problemsetQuestionList",
        )
        questions = (data or {}).get("problemsetQuestionList", {}).get("questions", [])
        out: list[ProblemSummary] = []
        for q in questions:
            out.append(
                ProblemSummary(
                    question_id=str(q["questionId"]),
                    frontend_id=str(q["questionFrontendId"]),
                    title=q["title"],
                    title_slug=q["titleSlug"],
                    difficulty=q.get("difficulty") or "Unknown",
                    status=q.get("status"),
                )
            )
        return out

    def get_problem(self, title_slug: str) -> ProblemDetail:
        data = self._gql(
            _QUESTION_QUERY,
            {"titleSlug": title_slug},
            operation_name="questionData",
        )
        q = (data or {}).get("question")
        if not q:
            raise LeetCodeError("Problem not found: %s" % title_slug)

        description = _html_to_text(q.get("content") or "")
        # The constraints field does not exist on QuestionNode in the current
        # schema; constraints are embedded inside the content HTML.
        constraints = _extract_constraints(description)

        starter_code = None
        for snippet in q.get("codeSnippets") or []:
            if (snippet.get("langSlug") or "") == self._lang:
                starter_code = snippet.get("code")
                break

        return ProblemDetail(
            question_id=str(q["questionId"]),
            frontend_id=str(q.get("questionFrontendId") or ""),
            title=q["title"],
            title_slug=q["titleSlug"],
            difficulty=q.get("difficulty") or "Unknown",
            is_paid=bool(q.get("isPaidOnly")),
            description_text=description,
            constraints_text=constraints,
            starter_code=starter_code,
            sample_test_case=q.get("sampleTestCase") or q.get("exampleTestcases") or "",
        )

    def submit(self, question_id: str, code: str, title_slug: str = "") -> str:
        """Submit a solution via the legacy REST endpoint.

        LeetCode removed the `submitSolution` GraphQL mutation, so submissions
        go through POST /problems/<slug>/submit/ (the same endpoint the website
        uses). Returns the submission id as a string.

        Handles two classes of failure:
        - HTTP 429: LeetCode submission rate limit. Backs off with increasing
          delays (30s, 60s, 120s) and retries.
        - HTTP 403 with Cloudflare "Just a moment" page: Cloudflare bot
          challenge triggered by too many rapid submissions. This is NOT an
          expired session — reads still work. Backs off and retries.
        """
        if not title_slug:
            raise LeetCodeError("submit requires a title_slug for the REST endpoint")
        self._ensure_csrf()
        url = f"{BASE_URL}/problems/{title_slug}/submit/"
        payload = {
            "lang": self._lang,
            "question_id": str(question_id),
            "test_mode": False,
            "judge_type": "large",
            "typed_code": code,
            "data_input": "",
        }
        headers = {"Referer": f"{BASE_URL}/problems/{title_slug}/"}

        # Backoff schedule: 30s, 60s, 120s, 240s. LeetCode's submission rate
        # limit is aggressive; short backoffs just waste retries.
        backoff_schedule = [30.0, 60.0, 120.0, 240.0]
        last_err: Optional[Exception] = None
        last_status: Optional[int] = None
        last_was_cloudflare = False
        for attempt in range(len(backoff_schedule)):
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=30)
            except requests.exceptions.RequestException as exc:
                last_err = exc
                wait = backoff_schedule[attempt]
                logger.warning("Submit network error; retrying in %.0fs", wait)
                time.sleep(wait)
                continue

            # Cloudflare bot challenge: 403 with "Just a moment..." HTML.
            # This is transient — back off and retry, don't treat as auth error.
            if resp.status_code == 403 and "just a moment" in resp.text.lower():
                last_status = 403
                last_was_cloudflare = True
                wait = backoff_schedule[attempt]
                logger.warning(
                    "Cloudflare bot challenge on submit (HTTP 403); backing off %.0fs",
                    wait,
                )
                time.sleep(wait)
                continue

            # True auth failure (not Cloudflare): 401, or 403 without the
            # Cloudflare challenge page.
            if resp.status_code == 401 or (
                resp.status_code == 403 and "just a moment" not in resp.text.lower()
            ):
                body = resp.text[:500] if resp.text else "(empty)"
                raise LeetCodeError(
                    "LeetCode rejected the submission (HTTP %d). The "
                    "LEETCODE_SESSION/csrftoken cookies are likely expired or "
                    "invalid. Body: %s" % (resp.status_code, body)
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                last_status = resp.status_code
                wait = backoff_schedule[attempt]
                logger.warning(
                    "Submit returned HTTP %d; backing off %.0fs",
                    resp.status_code,
                    wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise LeetCodeError(
                    "Submit failed: HTTP %d %s" % (resp.status_code, resp.text[:500])
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise LeetCodeError("Submit returned non-JSON: %s" % resp.text[:500])
            submission_id = data.get("submission_id")
            if submission_id is None:
                raise LeetCodeError("Submit returned no submission_id: %s" % data)
            return str(submission_id)
        if last_was_cloudflare:
            raise LeetCodeError(
                "Cloudflare bot challenge on submit (HTTP 403) after %d retries. "
                "LeetCode is blocking automated submissions — wait several minutes "
                "and re-run." % len(backoff_schedule)
            )
        if last_status == 429:
            raise LeetCodeError(
                "Submit rate-limited (HTTP 429) after %d retries. LeetCode is "
                "throttling submissions — wait several minutes and re-run."
                % len(backoff_schedule)
            )
        raise LeetCodeError(
            "Submit failed after %d retries (last error: %s). LeetCode may be "
            "rate-limiting submissions — wait a while and re-run."
            % (len(backoff_schedule), last_err)
        )

    def poll_submission(
        self,
        submission_id: str,
        timeout: float = 60.0,
        interval: float = 2.0,
    ) -> SubmissionResult:
        """Poll until the judge finishes or timeout elapses."""
        deadline = time.time() + timeout
        last: Optional[dict] = None
        while time.time() < deadline:
            data = self._gql(
                _SUBMISSION_DETAILS_QUERY,
                {"submissionId": int(submission_id)},
                operation_name="submissionDetails",
            )
            detail = (data or {}).get("submissionDetails")
            if detail:
                last = detail
                status = (detail.get("statusDisplay") or "").strip()
                # statusCode: 10..14 typically mean "started"/"pending".
                code = detail.get("statusCode")
                pending = code in (None, 10, 11, 12, 13, 14) and not status
                # "Internal Error" is a transient judge state — the verdict
                # often flips to a real status (Accepted/WA) moments later.
                # Keep polling rather than recording a bogus failure.
                if status == "Internal Error":
                    pending = True
                if not pending and status:
                    return SubmissionResult(
                        submission_id=str(detail.get("id") or submission_id),
                        status_display=status,
                        runtime=detail.get("runtime"),
                        memory=detail.get("memory"),
                        pending=False,
                    )
            time.sleep(interval)
        # Timed out — return whatever we have, marked pending.
        return SubmissionResult(
            submission_id=str(submission_id),
            status_display=(last or {}).get("statusDisplay") or "Unknown",
            runtime=(last or {}).get("runtime"),
            memory=(last or {}).get("memory"),
            pending=True,
        )


# --- helpers ----------------------------------------------------------------


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Preserve some structure for readability.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all(["p", "div", "li", "pre"]):
        p.append("\n")
    text = soup.get_text()
    # Collapse excessive blank lines.
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    return "\n".join(out).strip()


def _extract_constraints(description: str) -> str:
    marker = "Constraints:"
    idx = description.rfind(marker)
    if idx == -1:
        return ""
    return description[idx + len(marker) :].strip()
