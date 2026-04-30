"""Deterministic helpers for transport-safe browser request normalization."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SLACK_LINK_RE = re.compile(
    r"<(?P<url>(?:https?|file)://[^>|]+)(?:\|[^>]+)?>",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"((?:https?|file)://[^\s]+)", flags=re.IGNORECASE)
_BARE_URL_RE = re.compile(
    r"\b((?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?)",
    flags=re.IGNORECASE,
)
_BROWSER_ACTION_HINTS = (
    "browser",
    "browse",
    "check ",
    "open ",
    "go to ",
    "navigate",
    "inspect",
    "summarize",
    "page",
    "website",
    "site",
    "url",
)
_TRAILING_URL_PUNCTUATION = ".,!?;:>)]}\"'"
_LEADING_URL_WRAPPERS = "<([{"
_KNOWN_SITE_URLS = {
    "cnn": "https://www.cnn.com",
    "espn": "https://www.espn.com",
    "wikipedia": "https://www.wikipedia.org",
}


@dataclass(frozen=True)
class BrowserRequestMatch:
    """A narrow, deterministic browser request match for obvious URL actions."""

    url: str
    action: str
    target: str | None = None


@dataclass(frozen=True)
class BrowserSafetyBlocker:
    """A human-readable reason a browser objective must not be automated."""

    reason: str
    next_action: str
    category: str


def detect_browser_safety_blocker(text: str) -> BrowserSafetyBlocker | None:
    """Block browser objectives that would cross auth, payment, or sensitive-form lines."""

    lowered = f" {' '.join(text.lower().split())} "
    checks: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
        (
            "captcha",
            ("captcha", "verify you are human", "human verification", "cloudflare challenge"),
            "I can't automate CAPTCHA or human-verification steps.",
            "Complete the verification yourself, then retry on a page that no longer requires it.",
        ),
        (
            "two_factor",
            ("2fa", "two-factor", "two factor", "verification code", "one-time code", "otp", "authenticator app"),
            "I can't automate 2FA or verification-code steps.",
            "Complete the verification yourself, then ask me to continue from the unlocked page.",
        ),
        (
            "payment",
            ("checkout", "payment", "purchase", "credit card", "card number", "billing", "place order", "buy now", "pay for"),
            "I can't automate purchases, payments, checkout, or billing forms.",
            "Handle the payment step yourself; I can inspect non-payment public pages.",
        ),
        (
            "credentials",
            (" log in ", " login ", "log into", "sign in", "sign into", "signin", "password", "username", "enter credentials", "type my password"),
            "I can't enter credentials or automate login flows.",
            "Log in yourself in the visible browser if it is open, then ask me to continue from the unlocked page.",
        ),
        (
            "sensitive_form",
            ("social security", " ssn ", "date of birth", "medical", "bank account", "routing number", "tax id"),
            "I can't automate sensitive personal, medical, tax, or banking forms.",
            "Review and complete sensitive forms yourself; I can help with non-sensitive page inspection.",
        ),
    )
    for category, markers, reason, next_action in checks:
        if any(marker in lowered for marker in markers):
            return BrowserSafetyBlocker(reason=reason, next_action=next_action, category=category)

    school_terms = ("school portal", "student portal", "canvas", "blackboard", "powerschool", "classroom")
    completion_terms = ("submit", "turn in", "complete assignment", "do my assignment", "take quiz", "take test", "fill out")
    if any(term in lowered for term in school_terms) and any(term in lowered for term in completion_terms):
        return BrowserSafetyBlocker(
            reason="I can't complete authenticated school-portal work or submissions on your behalf.",
            next_action="You stay present for any auth or submission step; I can help inspect safe, non-submission pages.",
            category="school_portal",
        )
    return None


def normalize_transport_text(text: str) -> str:
    """Normalize platform markup without changing ordinary user wording."""
    normalized = text.replace("\r\n", "\n")
    return _SLACK_LINK_RE.sub(lambda match: match.group("url"), normalized)


def sanitize_url_candidate(raw_url: str | None) -> str | None:
    """Strip transport wrappers and trailing punctuation from a URL candidate."""
    if raw_url is None:
        return None
    candidate = raw_url.strip()
    if not candidate:
        return None

    slack_match = _SLACK_LINK_RE.fullmatch(candidate)
    if slack_match:
        candidate = slack_match.group("url")
    elif candidate.startswith("<") and candidate.endswith(">"):
        inner = candidate[1:-1]
        if "|" in inner:
            inner = inner.split("|", 1)[0]
        candidate = inner
    elif "|" in candidate:
        candidate = candidate.split("|", 1)[0]

    while candidate and candidate[0] in _LEADING_URL_WRAPPERS:
        candidate = candidate[1:]
    while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
        candidate = candidate[:-1]

    if not candidate:
        return None
    if candidate.lower().startswith("www."):
        return f"https://{candidate}"
    if "://" not in candidate and "." in candidate and " " not in candidate:
        return f"https://{candidate}"
    return candidate


def extract_first_url(text: str) -> str | None:
    """Find and sanitize the first explicit or bare URL in text."""
    explicit = _URL_RE.search(text)
    if explicit:
        return sanitize_url_candidate(explicit.group(1))
    bare = _BARE_URL_RE.search(text)
    if bare:
        return sanitize_url_candidate(bare.group(1))
    return None


def extract_obvious_browser_request(text: str) -> BrowserRequestMatch | None:
    """Return a deterministic browser request only for obvious URL-based asks."""
    lowered = text.lower().strip()
    if not any(token in lowered for token in _BROWSER_ACTION_HINTS):
        return None
    url = extract_first_url(text)
    target = None
    if url is None:
        target, url = resolve_known_browser_target(text)
    if url is None:
        return None
    return BrowserRequestMatch(url=url, action=_infer_browser_action(lowered), target=target or url)


def resolve_known_browser_target(text: str) -> tuple[str | None, str | None]:
    """Resolve a few safe, deterministic site aliases without using the LLM."""

    lowered = f" {text.lower()} "
    for alias, url in _KNOWN_SITE_URLS.items():
        if f" {alias} " in lowered:
            return alias, url
    return None, None


def _infer_browser_action(lowered_text: str) -> str:
    if "inspect" in lowered_text:
        return "inspect"
    if "summarize" in lowered_text:
        return "summarize"
    return "open"
