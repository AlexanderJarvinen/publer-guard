"""
Deterministic linter — the ground truth for publer-guard.

Every function is a pure rule: takes data in, returns Violations out.
No LLM calls, no side effects, no I/O.

Entry points:
  lint_row(row)            -> run all per-row rules
  lint_all(raw_rows, rows) -> column_count + all per-row rules
"""

from __future__ import annotations

import re
from datetime import datetime

from .state import CsvRow, Platform, Severity, Violation, _extract_hashtags

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATE_FORMAT = "%Y/%m/%d %H:%M"
_EXPECTED_COLUMNS = 6
_TWITTER_MAX_CHARS = 280

# 2084 series: Label contains this string (case-insensitive) — user-tunable here
_SERIES_2084_MARKER = "2084"

# Baseline required hashtag set for 2084 series posts
_REQUIRED_2084_HASHTAGS: frozenset[str] = frozenset({
    "#arcticdreams",
    "#2084",
    "#orwell",
    "#extrememetal",
    "#blackeneddeathmetal",
})

_CTA_HANDLE = "alex_y_yarvinen"
_CTA_FLAT_URL = f"instagram.com/{_CTA_HANDLE}"
_CTA_NATIVE = f"@{_CTA_HANDLE}"

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


# ---------------------------------------------------------------------------
# Rule 1 — column_count  (structural: operates on raw CSV data lines)
# ---------------------------------------------------------------------------

def check_column_count(raw_rows: list[list[str]]) -> list[Violation]:
    """
    Each data row must have exactly 6 fields.
    raw_rows: list of already-split CSV data lines (header excluded).
    row_index matches the position in this list (0-based).
    """
    violations: list[Violation] = []
    for row_index, fields in enumerate(raw_rows):
        if len(fields) != _EXPECTED_COLUMNS:
            violations.append(Violation(
                rule_id="column_count",
                row_index=row_index,
                severity=Severity.ERROR,
                message=(
                    f"Row {row_index} has {len(fields)} columns; "
                    f"expected {_EXPECTED_COLUMNS} "
                    f"(Date, Text, Link, Media URL, Title, Label)."
                ),
                auto_fixable=False,
            ))
    return violations


# ---------------------------------------------------------------------------
# Rule 2 — date_format
# ---------------------------------------------------------------------------

def check_date_format(row: CsvRow) -> list[Violation]:
    """Date must parse as YYYY/MM/DD HH:MM."""
    try:
        datetime.strptime(row.date.strip(), _DATE_FORMAT)
        return []
    except ValueError:
        return [Violation(
            rule_id="date_format",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: date {row.date!r} is not in "
                f"'YYYY/MM/DD HH:MM' format (e.g. '2026/06/24 18:00')."
            ),
            auto_fixable=True,
        )]


# ---------------------------------------------------------------------------
# Rule 3 — link_empty
# ---------------------------------------------------------------------------

def check_link_empty(row: CsvRow) -> list[Violation]:
    """The Link column must be empty — a non-empty value triggers Publer import errors."""
    if row.link.strip():
        return [Violation(
            rule_id="link_empty",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: Link column must be empty but contains "
                f"{row.link!r}. Non-empty Link causes 'Invalid URL attached' in Publer."
            ),
            auto_fixable=True,
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 4 — media_url_permanent
# ---------------------------------------------------------------------------

def check_media_url_permanent(row: CsvRow) -> list[Violation]:
    """Media URL must not contain /uploads/tmp/ — temporary links expire before publish."""
    if "/uploads/tmp/" in row.media_url:
        return [Violation(
            rule_id="media_url_permanent",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: Media URL is a temporary link "
                f"(contains '/uploads/tmp/'). Must be resolved via lookup_media."
            ),
            auto_fixable=True,   # requires lookup_media tool; escalates to human if not found
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 5 — twitter_length
# ---------------------------------------------------------------------------

def check_twitter_length(row: CsvRow) -> list[Violation]:
    """Twitter posts must be at most 280 characters."""
    if row.platform != Platform.TWITTER:
        return []
    length = len(row.text)
    if length > _TWITTER_MAX_CHARS:
        return [Violation(
            rule_id="twitter_length",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: Twitter post is {length} chars; "
                f"limit is {_TWITTER_MAX_CHARS}. Over by {length - _TWITTER_MAX_CHARS} chars."
            ),
            auto_fixable=True,
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 6 — cta_format
# ---------------------------------------------------------------------------

def _cta_present(text: str) -> bool:
    """True if the text contains any reference to the CTA handle."""
    return _CTA_HANDLE in text


def _has_markdown_link(text: str) -> bool:
    """True if text contains a markdown link pointing to the CTA Instagram profile."""
    pattern = (
        r"\[.+?\]\(https?://(?:www\.)?instagram\.com/"
        + re.escape(_CTA_HANDLE)
        + r"[^)]*\)"
    )
    return bool(re.search(pattern, text))


def _has_flat_url(text: str) -> bool:
    """
    True if text contains the CTA as a bare URL (not inside a markdown link).
    Strips markdown link syntax first to avoid false positives.
    """
    # Remove markdown links so we don't match the URL inside [text](url)
    stripped = re.sub(r"\[.+?\]\(https?://[^)]+\)", "", text)
    return bool(re.search(
        r"(?:IG:\s*)?instagram\.com/" + re.escape(_CTA_HANDLE),
        stripped,
    ))


def _has_native_mention(text: str) -> bool:
    return _CTA_NATIVE in text


def check_cta_format(row: CsvRow) -> list[Violation]:
    """
    CTA format must match the platform convention:
    - TWITTER / FACEBOOK: flat URL only, e.g. 'IG: instagram.com/alex_y_yarvinen'
    - TELEGRAM: flat URL or markdown link, both allowed
    - INSTAGRAM_BAND: native @handle only
    - INSTAGRAM_FUNNEL: no CTA permitted at all
    Other platforms (YOUTUBE, TIKTOK): no CTA rule enforced.
    """
    text = row.text
    platform = row.platform

    if platform == Platform.INSTAGRAM_FUNNEL:
        if _cta_present(text):
            return [Violation(
                rule_id="cta_format",
                row_index=row.row_index,
                severity=Severity.ERROR,
                message=(
                    f"Row {row.row_index}: INSTAGRAM_FUNNEL posts must carry no CTA, "
                    f"but text references '{_CTA_HANDLE}'."
                ),
                auto_fixable=True,
            )]
        return []

    if platform in (Platform.TWITTER, Platform.FACEBOOK):
        if not _cta_present(text):
            return []
        if _has_flat_url(text) and not _has_markdown_link(text):
            return []   # correct flat-URL format
        return [Violation(
            rule_id="cta_format",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: {platform.value} CTA must be a plain URL "
                f"(e.g. 'IG: {_CTA_FLAT_URL}'), not a markdown link or @mention."
            ),
            auto_fixable=True,
        )]

    if platform == Platform.TELEGRAM:
        if not _cta_present(text):
            return []
        if _has_flat_url(text) or _has_markdown_link(text):
            return []   # both formats allowed on Telegram
        return [Violation(
            rule_id="cta_format",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: TELEGRAM CTA must be a flat URL or markdown link; "
                f"found unrecognised format (e.g. bare @mention is not allowed)."
            ),
            auto_fixable=True,
        )]

    if platform == Platform.INSTAGRAM_BAND:
        if not _cta_present(text):
            return []
        if _has_native_mention(text):
            return []   # @handle is the correct format
        return [Violation(
            rule_id="cta_format",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: INSTAGRAM_BAND CTA must be the native mention "
                f"'{_CTA_NATIVE}', not a URL or markdown link."
            ),
            auto_fixable=True,
        )]

    # YOUTUBE, TIKTOK — no CTA rule
    return []


# ---------------------------------------------------------------------------
# Rule 7 — hashtags_2084
# ---------------------------------------------------------------------------

def _is_2084_row(row: CsvRow) -> bool:
    # Membership determined solely by Label containing "2084" (case-insensitive)
    return _SERIES_2084_MARKER.lower() in row.label.lower()


def check_hashtags_2084(row: CsvRow) -> list[Violation]:
    """2084 series rows must contain all required hashtags."""
    if not _is_2084_row(row):
        return []
    present = {h.lower() for h in _extract_hashtags(row.text)}
    missing = _REQUIRED_2084_HASHTAGS - present
    if missing:
        return [Violation(
            rule_id="hashtags_2084",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: 2084 series post is missing required hashtags: "
                f"{', '.join(sorted(missing))}."
            ),
            auto_fixable=True,
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 8 — no_cyrillic (except Telegram)
# ---------------------------------------------------------------------------

def check_no_cyrillic(row: CsvRow) -> list[Violation]:
    """
    Latin script required everywhere EXCEPT Telegram.

    The Telegram channel is intentionally Russian-language, so Cyrillic is
    legitimate there. On every other platform Cyrillic indicates an error
    (e.g. Serbian text accidentally left in Cyrillic instead of Latin).

    Note: this is deliberately the narrow, mechanically-checkable invariant.
    Full Serbian-diacritics validation would require language detection,
    which can't be grounded without an LLM judgment — so it's out of scope
    on purpose. Cyrillic presence is a fact; "is this correct Serbian" is not.
    """
    if row.platform == Platform.TELEGRAM:
        return []   # Russian-language channel — Cyrillic is expected
    if _CYRILLIC_RE.search(row.text):
        return [Violation(
            rule_id="no_cyrillic",
            row_index=row.row_index,
            severity=Severity.ERROR,
            message=(
                f"Row {row.row_index}: text contains Cyrillic on "
                f"{row.platform.value}, which expects Latin script. "
                f"(Cyrillic is only allowed on Telegram.)"
            ),
            auto_fixable=True,
        )]
    return []


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

_PER_ROW_CHECKS = (
    check_date_format,
    check_link_empty,
    check_media_url_permanent,
    check_twitter_length,
    check_cta_format,
    check_hashtags_2084,
    check_no_cyrillic,
)


def lint_row(row: CsvRow) -> list[Violation]:
    """Run all per-row rules against a single CsvRow. Used by the Verifier."""
    violations: list[Violation] = []
    for check in _PER_ROW_CHECKS:
        violations.extend(check(row))
    return violations


def lint_all(raw_rows: list[list[str]], rows: list[CsvRow]) -> list[Violation]:
    """
    Full lint pass over a parsed CSV.
    raw_rows: list of split data lines (header excluded), one per data row.
    rows: corresponding CsvRow objects (same order, same length).
    """
    violations: list[Violation] = []
    violations.extend(check_column_count(raw_rows))
    for row in rows:
        violations.extend(lint_row(row))
    return violations
