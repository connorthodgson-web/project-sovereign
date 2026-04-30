"""Explicit contact memory parsing and normalization."""

from __future__ import annotations

from dataclasses import dataclass
import re

from memory.safety import looks_secret_like


EMAIL_RE = r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"

_UNSAFE_ALIAS_MARKERS = {
    "api key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "access token",
    "refresh token",
    "client secret",
    "credentials",
    "credential",
    "gmail token",
    "oauth token",
}

_AMBIGUOUS_SELF_ALIASES = {
    "i",
    "me",
    "my",
    "mine",
    "my email",
    "email",
    "address",
    "the email",
    "this",
    "that",
}


@dataclass(frozen=True)
class ParsedContactStatement:
    """A contact alias/email pair explicitly provided by the user."""

    alias: str
    email: str


def parse_explicit_contact_statement(message: str) -> ParsedContactStatement | None:
    """Extract safe contact memory only from explicit alias + email statements."""

    normalized = " ".join(message.strip().replace("’", "'").split())
    if not normalized or looks_secret_like(normalized):
        return None

    patterns = (
        rf"^(?:remember that\s+)?(?P<alias>[A-Za-z][A-Za-z .'\-]{{0,80}})'s\s+(?:email|email address)\s+(?:is|=|:|changed to|is now|should be)\s+(?P<email>{EMAIL_RE})[.!?]?$",
        rf"^(?:remember that\s+)?(?:update|change)\s+(?P<alias>[A-Za-z][A-Za-z .'\-]{{0,80}})'s\s+(?:email|email address)\s+(?:to|as)\s+(?P<email>{EMAIL_RE})[.!?]?$",
        rf"^(?:remember that\s+)?use\s+(?P<email>{EMAIL_RE})\s+for\s+(?P<alias>[A-Za-z][A-Za-z .'\-]{{0,80}})[.!?]?$",
        rf"^(?:remember that\s+)?(?P<alias>[A-Za-z][A-Za-z .'\-]{{0,80}})\s+(?:is|=)\s+(?P<email>{EMAIL_RE})[.!?]?$",
    )
    for pattern in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        alias = clean_contact_alias(match.group("alias"))
        email = match.group("email").strip(" .").lower()
        if alias and is_email_address(email):
            return ParsedContactStatement(alias=alias, email=email)
    return None


def clean_contact_alias(value: str) -> str:
    """Return a display-safe contact alias or an empty string if unsafe."""

    cleaned = " ".join(value.strip().strip(" .'\"").split())
    cleaned = re.sub(r"^(?:remember that|use)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    lowered = cleaned.lower()
    if (
        not cleaned
        or len(cleaned.split()) > 5
        or "@" in cleaned
        or lowered in _AMBIGUOUS_SELF_ALIASES
        or any(marker in lowered for marker in _UNSAFE_ALIAS_MARKERS)
        or looks_secret_like(cleaned)
    ):
        return ""
    return cleaned


def normalize_contact_key(value: str) -> str:
    cleaned = clean_contact_alias(value).lower()
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")


def is_email_address(value: str) -> bool:
    return bool(re.fullmatch(EMAIL_RE, value.strip()))
