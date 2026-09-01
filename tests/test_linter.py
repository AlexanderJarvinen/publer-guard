"""
Tests for src/linter.py — one passing + one failing case per rule, plus
edge cases for rules with platform-specific branching.

Design contract: every test asserts on rule_id so it fails if the wrong
rule fires. Tests must catch a broken rule without knowing how it is broken.
"""


from src.linter import (
    check_column_count,
    check_cta_format,
    check_date_format,
    check_link_empty,
    check_media_url_permanent,
    check_no_cyrillic,
    check_twitter_length,
    lint_row,
)
from src.state import CsvRow, Platform

# ---------------------------------------------------------------------------
# Fixture helper — build a minimal valid CsvRow; override with kwargs
# ---------------------------------------------------------------------------

def _row(**kwargs) -> CsvRow:
    defaults = dict(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026/06/24 18:00",
        text="Post text.",
        link="",
        media_url="https://cdn.publer.com/uploads/videos/abc/def.mp4",
        title="Title",
        label="promo",
    )
    defaults.update(kwargs)
    return CsvRow(**defaults)


# ===========================================================================
# Rule 1 — column_count
# ===========================================================================

class TestColumnCount:
    def test_exactly_twelve_columns_is_clean(self):
        assert check_column_count([["a"] * 12]) == []

    def test_five_columns_fires(self):
        v = check_column_count([["a", "b", "c", "d", "e"]])
        assert len(v) == 1
        assert v[0].rule_id == "column_count"
        assert v[0].row_index == 0

    def test_thirteen_columns_fires(self):
        v = check_column_count([["a"] * 13])
        assert len(v) == 1
        assert v[0].rule_id == "column_count"

    def test_only_bad_rows_fire_in_multi_row_input(self):
        raw = [
            ["a"] * 12,      # OK — 12 columns (the Publer template)
            ["x", "y"],      # bad — row 1
            ["a"] * 12,      # OK — 12 columns
        ]
        v = check_column_count(raw)
        assert len(v) == 1
        assert v[0].row_index == 1

    def test_all_bad_rows_fire(self):
        raw = [["a"], ["b", "c"]]
        v = check_column_count(raw)
        assert len(v) == 2

    def test_empty_input_returns_no_violations(self):
        assert check_column_count([]) == []

    def test_auto_fixable_is_false(self):
        v = check_column_count([["a"]])
        assert v[0].auto_fixable is False


# ===========================================================================
# Rule 2 — date_format
# ===========================================================================

class TestDateFormat:
    def test_valid_date_is_clean(self):
        assert check_date_format(_row(date="2026/06/24 18:00")) == []

    def test_us_format_fires(self):
        v = check_date_format(_row(date="06/24/2026 20:00"))
        assert len(v) == 1
        assert v[0].rule_id == "date_format"

    def test_dash_format_fires(self):
        # ISO dash format is not the Publer format — only YYYY/MM/DD is accepted
        v = check_date_format(_row(date="2026-06-24 18:00"))
        assert len(v) == 1
        assert v[0].rule_id == "date_format"

    def test_iso_8601_with_T_fires(self):
        v = check_date_format(_row(date="2026/06/24T18:00"))
        assert len(v) == 1
        assert v[0].rule_id == "date_format"

    def test_date_without_a_time_is_clean(self):
        """Leaving the hour out is an editorial choice — the post is dated and
        gets scheduled by hand in Publer."""
        assert check_date_format(_row(date="2026/06/24")) == []

    def test_an_empty_date_still_fires(self):
        """The time is optional; the date is not — there is nothing to
        schedule without one."""
        v = check_date_format(_row(date=""))
        assert len(v) == 1
        assert v[0].rule_id == "date_format"

    def test_a_date_with_a_broken_time_fires(self):
        """Half a time is a mistake, not a choice."""
        for broken in ("2026/06/24 18", "2026/06/24 18:", "2026/06/24 25:00"):
            v = check_date_format(_row(date=broken))
            assert len(v) == 1, broken

    def test_leading_trailing_whitespace_is_tolerated(self):
        assert check_date_format(_row(date="  2026/06/24 18:00  ")) == []
        assert check_date_format(_row(date="  2026/06/24  ")) == []

    def test_midnight_is_valid(self):
        assert check_date_format(_row(date="2026/01/01 00:00")) == []

    def test_row_index_propagated(self):
        v = check_date_format(_row(row_index=7, date="bad"))
        assert v[0].row_index == 7


# ===========================================================================
# Rule 3 — link_empty
# ===========================================================================

class TestLinkEmpty:
    def test_empty_string_is_clean(self):
        assert check_link_empty(_row(link="")) == []

    def test_whitespace_only_is_clean(self):
        assert check_link_empty(_row(link="   ")) == []

    def test_url_fires(self):
        v = check_link_empty(_row(link="https://ffm.to/arctic"))
        assert len(v) == 1
        assert v[0].rule_id == "link_empty"

    def test_any_nonempty_value_fires(self):
        v = check_link_empty(_row(link="x"))
        assert len(v) == 1
        assert v[0].rule_id == "link_empty"

    def test_auto_fixable_is_true(self):
        v = check_link_empty(_row(link="https://example.com"))
        assert v[0].auto_fixable is True


# ===========================================================================
# Rule 4 — media_url_permanent
# ===========================================================================

class TestMediaUrlPermanent:
    def test_cdn_url_is_clean(self):
        assert check_media_url_permanent(_row(
            media_url="https://cdn.publer.com/uploads/videos/abc/def.mp4"
        )) == []

    def test_empty_media_url_is_clean(self):
        assert check_media_url_permanent(_row(media_url="")) == []

    def test_tmp_url_fires(self):
        v = check_media_url_permanent(_row(
            media_url="https://app.publer.com/uploads/tmp/1781206811/file.mp4"
        ))
        assert len(v) == 1
        assert v[0].rule_id == "media_url_permanent"

    def test_auto_fixable_is_true(self):
        v = check_media_url_permanent(_row(
            media_url="https://app.publer.com/uploads/tmp/x/y.mp4"
        ))
        assert v[0].auto_fixable is True

    def test_tmp_anywhere_in_url_fires(self):
        # Ensure it catches the pattern wherever it appears
        v = check_media_url_permanent(_row(
            media_url="https://app.publer.com/uploads/tmp/12345-67890/hash.mov"
        ))
        assert len(v) == 1


# ===========================================================================
# Rule 5 — twitter_length
# ===========================================================================

class TestTwitterLength:
    def test_exactly_280_chars_is_clean(self):
        assert check_twitter_length(_row(platform=Platform.TWITTER, text="x" * 280)) == []

    def test_281_chars_fires(self):
        v = check_twitter_length(_row(platform=Platform.TWITTER, text="x" * 281))
        assert len(v) == 1
        assert v[0].rule_id == "twitter_length"

    def test_over_limit_message_contains_overage(self):
        v = check_twitter_length(_row(platform=Platform.TWITTER, text="x" * 290))
        assert "10" in v[0].message  # over by 10

    def test_rule_not_applied_to_facebook(self):
        assert check_twitter_length(_row(platform=Platform.FACEBOOK, text="x" * 500)) == []

    def test_rule_not_applied_to_instagram_band(self):
        assert check_twitter_length(_row(platform=Platform.INSTAGRAM_BAND, text="x" * 500)) == []

    def test_rule_not_applied_to_telegram(self):
        assert check_twitter_length(_row(platform=Platform.TELEGRAM, text="x" * 500)) == []

    def test_empty_twitter_text_is_clean(self):
        assert check_twitter_length(_row(platform=Platform.TWITTER, text="")) == []


# ===========================================================================
# Rule 6 — cta_format
# ===========================================================================

class TestCtaFormat:

    # --- Twitter ---

    def test_twitter_flat_url_with_ig_prefix_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TWITTER,
            text="Check it out. IG: instagram.com/alex_y_yarvinen #music",
        )) == []

    def test_twitter_flat_url_without_prefix_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TWITTER,
            text="Follow at instagram.com/alex_y_yarvinen for more.",
        )) == []

    def test_twitter_no_cta_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TWITTER,
            text="New track out now! #metal",
        )) == []

    def test_twitter_markdown_link_fires(self):
        v = check_cta_format(_row(
            platform=Platform.TWITTER,
            text="Follow [us](https://instagram.com/alex_y_yarvinen) for more.",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    def test_twitter_native_mention_fires(self):
        v = check_cta_format(_row(
            platform=Platform.TWITTER,
            text="Follow @alex_y_yarvinen!",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    # --- Facebook (same rules as Twitter) ---

    def test_facebook_flat_url_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.FACEBOOK,
            text="IG: instagram.com/alex_y_yarvinen",
        )) == []

    def test_facebook_markdown_fires(self):
        v = check_cta_format(_row(
            platform=Platform.FACEBOOK,
            text="[Follow](https://instagram.com/alex_y_yarvinen)",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    # --- Telegram: both flat and markdown are OK ---

    def test_telegram_flat_url_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TELEGRAM,
            text="IG: instagram.com/alex_y_yarvinen",
        )) == []

    def test_telegram_markdown_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TELEGRAM,
            text="Follow [Alex](https://instagram.com/alex_y_yarvinen) for more.",
        )) == []

    def test_telegram_no_cta_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.TELEGRAM,
            text="New release is out.",
        )) == []

    def test_telegram_native_mention_only_fires(self):
        # @mention alone is not a flat URL or markdown link
        v = check_cta_format(_row(
            platform=Platform.TELEGRAM,
            text="Follow @alex_y_yarvinen!",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    # --- INSTAGRAM_BAND: native @mention only ---

    def test_instagram_band_native_mention_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.INSTAGRAM_BAND,
            text="Follow @alex_y_yarvinen for more.",
        )) == []

    def test_instagram_band_no_cta_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.INSTAGRAM_BAND,
            text="New track out.",
        )) == []

    def test_instagram_band_flat_url_fires(self):
        v = check_cta_format(_row(
            platform=Platform.INSTAGRAM_BAND,
            text="IG: instagram.com/alex_y_yarvinen",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    def test_instagram_band_markdown_fires(self):
        v = check_cta_format(_row(
            platform=Platform.INSTAGRAM_BAND,
            text="[Follow](https://instagram.com/alex_y_yarvinen)",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    # --- INSTAGRAM_FUNNEL: zero CTA permitted ---

    def test_instagram_funnel_no_cta_is_clean(self):
        assert check_cta_format(_row(
            platform=Platform.INSTAGRAM_FUNNEL,
            text="New chapter is live. Listen now.",
        )) == []

    def test_instagram_funnel_native_mention_fires(self):
        v = check_cta_format(_row(
            platform=Platform.INSTAGRAM_FUNNEL,
            text="Follow @alex_y_yarvinen for more.",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    def test_instagram_funnel_flat_url_fires(self):
        v = check_cta_format(_row(
            platform=Platform.INSTAGRAM_FUNNEL,
            text="IG: instagram.com/alex_y_yarvinen",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    def test_instagram_funnel_markdown_fires(self):
        v = check_cta_format(_row(
            platform=Platform.INSTAGRAM_FUNNEL,
            text="[Follow](https://instagram.com/alex_y_yarvinen)",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "cta_format"

    # --- Platforms with no CTA rule ---

    def test_youtube_cta_present_no_violation(self):
        assert check_cta_format(_row(
            platform=Platform.YOUTUBE,
            text="instagram.com/alex_y_yarvinen",
        )) == []

    def test_tiktok_cta_present_no_violation(self):
        assert check_cta_format(_row(
            platform=Platform.TIKTOK,
            text="@alex_y_yarvinen",
        )) == []


# ===========================================================================
# Rule 7 — no_cyrillic
# ===========================================================================

class TestNoCyrillic:
    def test_latin_text_is_clean(self):
        assert check_no_cyrillic(_row(text="New chapter is live.")) == []

    def test_latin_with_diacritics_is_clean(self):
        assert check_no_cyrillic(_row(text="Novo poglavlje je objavljeno. Idemo!")) == []

    def test_cyrillic_on_facebook_fires(self):
        v = check_no_cyrillic(_row(platform=Platform.FACEBOOK, text="Нова глава је"))
        assert len(v) == 1
        assert v[0].rule_id == "no_cyrillic"

    def test_cyrillic_on_twitter_fires(self):
        v = check_no_cyrillic(_row(platform=Platform.TWITTER, text="Новая глава"))
        assert len(v) == 1
        assert v[0].rule_id == "no_cyrillic"

    def test_cyrillic_on_instagram_band_fires(self):
        v = check_no_cyrillic(_row(platform=Platform.INSTAGRAM_BAND, text="Привет мир"))
        assert len(v) == 1
        assert v[0].rule_id == "no_cyrillic"

    def test_cyrillic_on_instagram_funnel_fires(self):
        v = check_no_cyrillic(_row(platform=Platform.INSTAGRAM_FUNNEL, text="Привет"))
        assert len(v) == 1
        assert v[0].rule_id == "no_cyrillic"

    def test_cyrillic_on_telegram_is_clean(self):
        # Telegram is a Russian-language channel; Cyrillic is legitimate there
        assert check_no_cyrillic(_row(
            platform=Platform.TELEGRAM,
            text="Новая глава вышла. Слушайте сейчас.",
        )) == []

    def test_mixed_latin_cyrillic_fires(self):
        v = check_no_cyrillic(_row(
            platform=Platform.FACEBOOK,
            text="New chapter — Нова глава",
        ))
        assert len(v) == 1
        assert v[0].rule_id == "no_cyrillic"

    def test_empty_text_is_clean(self):
        assert check_no_cyrillic(_row(text="")) == []

    def test_auto_fixable_is_true(self):
        v = check_no_cyrillic(_row(platform=Platform.FACEBOOK, text="Привет"))
        assert v[0].auto_fixable is True


# ===========================================================================
# Integration — lint_row applies all per-row rules
# ===========================================================================

class TestLintRow:
    def test_fully_clean_row_returns_empty(self):
        r = _row(
            platform=Platform.TWITTER,
            date="2026/06/24 18:00",
            text="New track. IG: instagram.com/alex_y_yarvinen #metal",
            link="",
            media_url="https://cdn.publer.com/uploads/videos/abc/def.mp4",
            label="promo",
        )
        assert lint_row(r) == []

    def test_multiple_violations_all_returned(self):
        r = _row(
            platform=Platform.TWITTER,
            date="06-24-2026 20:00",                                        # bad date
            text="x" * 300,                                                 # too long
            link="https://ffm.to/arctic",                                   # non-empty link
            media_url="https://app.publer.com/uploads/tmp/x/y.mp4",        # temp URL
            label="promo",
        )
        rule_ids = {v.rule_id for v in lint_row(r)}
        assert "date_format" in rule_ids
        assert "link_empty" in rule_ids
        assert "media_url_permanent" in rule_ids
        assert "twitter_length" in rule_ids

    def test_cyrillic_on_facebook_fires_via_lint_row(self):
        r = _row(platform=Platform.FACEBOOK, text="Нова глава је објављена")
        rule_ids = {v.rule_id for v in lint_row(r)}
        assert "no_cyrillic" in rule_ids

    def test_cyrillic_on_telegram_clean_via_lint_row(self):
        r = _row(
            platform=Platform.TELEGRAM,
            text="Новая глава вышла. IG: instagram.com/alex_y_yarvinen",
        )
        rule_ids = {v.rule_id for v in lint_row(r)}
        assert "no_cyrillic" not in rule_ids

    def test_all_violations_carry_correct_row_index(self):
        r = _row(
            row_index=5,
            platform=Platform.TWITTER,
            date="bad-date",
            link="notempty",
        )
        for v in lint_row(r):
            assert v.row_index == 5
