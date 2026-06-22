"""
tools.py — deterministic tools the Fixer agent may call.

These are ground-truth lookups, not LLM calls. They are the mechanism behind
Gate 3 (no fabrication): the only legal way a Fixer can change a media URL is
by calling lookup_media and receiving a real permanent path back.

REAL PROD BUILD: replace the fixture read in lookup_media with a real HTTP call:
  GET https://app.publer.com/api/v1/media
  Headers:
    Authorization: Bearer-API <token>
    Publer-Workspace-Id: <workspace_id>
  The function signature stays identical; only the body changes.
"""

from __future__ import annotations

import json
from pathlib import Path

# Default fixture — relative to this file so it works from any working directory
_DEFAULT_FIXTURE = Path(__file__).parent.parent / "fixtures" / "media_library.json"


def lookup_media(hint: str, _fixture_path: Path | None = None) -> str | None:
    """
    Find a permanent Media URL in the Publer media library by hint.

    Searches fixture entries where `hint` appears in the entry's `name`
    (case-insensitive) OR in its `path`. Returns the permanent
    cdn.publer.com URL only when exactly ONE entry matches.

    Returns None when:
    - zero entries match (not found → escalate to human)
    - more than one entry matches (ambiguous → do not guess, escalate to human)
    - the matched path is a temporary link or not on cdn.publer.com
      (defensive: the fixture should never contain tmp links, but we verify)

    The `_fixture_path` kwarg is for testing only; omit it in all real call sites.
    """
    fixture_path = _fixture_path or _DEFAULT_FIXTURE

    try:
        raw = fixture_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"lookup_media: fixture not found at {fixture_path}. "
            "Ensure fixtures/media_library.json exists."
        )

    try:
        data = json.loads(raw)
        entries: list[dict] = data["media"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ValueError(
            f"lookup_media: fixture at {fixture_path} is malformed — {exc}"
        )

    hint_lower = hint.lower()
    matches = [
        e for e in entries
        if hint_lower in e.get("name", "").lower()
        or hint_lower in e.get("path", "")
    ]

    # Exactly one match required — zero or multiple both escalate to human
    if len(matches) != 1:
        return None

    path: str = matches[0].get("path", "")

    # Defensive check: never return a temporary or non-CDN URL even if somehow
    # present in the fixture data
    if "/uploads/tmp/" in path or "cdn.publer.com" not in path:
        return None

    return path
