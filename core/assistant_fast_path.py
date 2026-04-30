"""Shared helpers for obvious lightweight assistant interactions."""

from __future__ import annotations

import re

from memory.contacts import parse_explicit_contact_statement


_GREETING_MESSAGES = {
    "hello",
    "hi",
    "hey",
    "yo",
    "hello there",
    "hey there",
    "good morning",
    "good afternoon",
    "good evening",
    "what's up",
    "whats up",
}

_THANKS_MESSAGES = {
    "thanks",
    "thank you",
    "thx",
    "thanks.",
    "thank you.",
}

_USER_MEMORY_QUESTIONS = (
    "what do you know about me",
    "what do you remember about me",
    "what did i tell you before",
    "what did i tell you earlier",
)

_PROJECT_MEMORY_QUESTIONS = (
    "what do you remember about this project",
    "what do you remember about project sovereign",
    "what do you remember about sovereign",
    "what do you know about project sovereign",
    "what do you know about sovereign",
    "what do you remember about how i want this project to feel",
)

_MEMORY_FOLLOW_UP_MARKERS = (
    "is that all",
    "anything else",
    "what else",
    "all you have",
    "in memory",
    "remember anything else",
)


def normalize_message(text: str) -> str:
    return " ".join(text.lower().strip().split())


def is_greeting_message(text: str) -> bool:
    return normalize_message(text) in _GREETING_MESSAGES


def is_thanks_message(text: str) -> bool:
    return normalize_message(text) in _THANKS_MESSAGES


def is_user_memory_question(text: str) -> bool:
    normalized = normalize_message(text)
    return any(phrase in normalized for phrase in _USER_MEMORY_QUESTIONS)


def is_project_memory_question(text: str) -> bool:
    normalized = normalize_message(text)
    return any(phrase in normalized for phrase in _PROJECT_MEMORY_QUESTIONS)


def is_memory_lookup(text: str) -> bool:
    normalized = normalize_message(text)
    return normalized.startswith(("what is my ", "what's my ", "where did i ", "where is my ", "what did i ")) or (
        "current priority" in normalized
        or (
            ("remember" in normalized or "know" in normalized)
            and any(
                token in normalized
                for token in ("about me", "about this project", "about project sovereign", "earlier")
            )
        )
    )


def is_memory_follow_up_phrase(text: str) -> bool:
    normalized = normalize_message(text)
    if not normalized:
        return False
    return any(marker in normalized for marker in _MEMORY_FOLLOW_UP_MARKERS)


def extract_name_value(text: str) -> str | None:
    stripped = " ".join(text.strip().split())
    patterns = (
        r"^(?:remember that\s+)?my name is\s+(?P<name>[A-Za-z][A-Za-z .'\-]{0,80})[.!?]?$",
        r"^(?:remember that\s+)?call me\s+(?P<name>[A-Za-z][A-Za-z .'\-]{0,80})[.!?]?$",
    )
    for pattern in patterns:
        match = re.match(pattern, stripped, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = " ".join(match.group("name").split()).strip(" .!?")
        if not candidate:
            return None
        parts = candidate.split()
        if 1 <= len(parts) <= 4:
            return candidate
    return None


def is_name_statement(text: str) -> bool:
    return extract_name_value(text) is not None


def is_explicit_memory_statement(text: str) -> bool:
    normalized = normalize_message(text)
    if parse_explicit_contact_statement(text) is not None:
        return True
    if normalized.startswith("remember that "):
        return True
    return bool(re.match(r"^(remember|note)\s+(my|that|this)\b", normalized))


def is_forget_name_statement(text: str) -> bool:
    normalized = normalize_message(text)
    return normalized in {
        "forget my name",
        "please forget my name",
        "delete my name",
        "remove my name",
        "don't remember my name",
        "do not remember my name",
    }


def is_short_personal_fact_statement(text: str) -> bool:
    normalized = normalize_message(text)
    if is_name_statement(normalized):
        return True
    fact_markers = (
        "my birthday is ",
        "i live in ",
        "i'm from ",
        "i am from ",
        "my pronouns are ",
    )
    return any(normalized.startswith(marker) for marker in fact_markers) and len(normalized.split()) <= 10


def is_obvious_assistant_fast_path(text: str) -> bool:
    normalized = normalize_message(text)
    return any(
        (
            is_greeting_message(normalized),
            is_thanks_message(normalized),
            is_name_statement(normalized),
            is_explicit_memory_statement(normalized),
            is_forget_name_statement(normalized),
            is_short_personal_fact_statement(normalized),
            is_user_memory_question(normalized),
            is_project_memory_question(normalized),
            is_memory_lookup(normalized),
        )
    )
