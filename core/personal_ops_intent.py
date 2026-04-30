"""Lightweight Personal Ops intent helpers."""

from __future__ import annotations


def looks_like_personal_list_request(message: str) -> bool:
    """Detect obvious personal list/note work without becoming the main router."""

    lowered = " ".join(message.lower().strip().split())
    if not lowered:
        return False
    if _looks_like_coding_or_browser(lowered):
        return False
    list_terms = (" list", " lists", " note", " notes")
    action_terms = (
        "add ",
        "make ",
        "create ",
        "remember this list",
        "remove ",
        "delete ",
        "rename ",
        "summarize ",
        "what's on ",
        "whats on ",
        "what is on ",
        "what did i tell you",
        "what classes did i tell you",
        "move ",
        "update ",
    )
    if any(term in lowered for term in list_terms) and any(term in lowered for term in action_terms):
        return True
    if "what classes did i tell you" in lowered:
        return True
    if lowered.startswith(("add ", "remove ", "update ")) and lowered.endswith((" too", " also")):
        return True
    return lowered in {"what's on that list", "whats on that list", "what is on that list", "summarize that list"}


def looks_like_proactive_routine_request(message: str) -> bool:
    lowered = " ".join(message.lower().strip().split())
    if not lowered:
        return False
    if "remind me" in lowered or "set a reminder" in lowered:
        return False
    routine_markers = (
        "every morning",
        "every sunday",
        "every week",
        "weekly",
        "daily",
        "each morning",
    )
    outcome_markers = (
        "summarize",
        "message me",
        "send me",
        "check my",
        "report",
        "routine",
    )
    return any(marker in lowered for marker in routine_markers) and any(marker in lowered for marker in outcome_markers)


def looks_like_personal_ops_request(message: str) -> bool:
    return looks_like_personal_list_request(message) or looks_like_proactive_routine_request(message)


def _looks_like_coding_or_browser(lowered: str) -> bool:
    return any(
        token in lowered
        for token in (
            "http://",
            "https://",
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".json",
            "workspace/",
            "open ",
            "browse ",
            "debug ",
            "refactor ",
            "compile",
            "pytest",
        )
    )
