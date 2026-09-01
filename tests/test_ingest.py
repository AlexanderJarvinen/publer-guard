"""
Tests for the content-plan slicer.

The spreadsheet backends (odfpy / xlrd) are exercised only indirectly: they
exist to produce a `_Grid`, so these tests build a `_Grid` by hand and stub
`_open`. That keeps the interesting logic — column mapping, the media gate,
text assembly, file naming — testable without binary fixtures.
"""

import datetime as dt
from pathlib import Path

import pytest

from src import ingest
from src.ingest import (
    EXPECTED_HEADERS,
    HEADER_ROWS,
    PlanLayoutError,
    _Cell,
    _col,
    _Grid,
    check_layout,
    checked_columns,
    has_media,
    slice_plan,
    units_reading,
)
from src.state import Platform

GRID_WIDTH = 100    # past CQ (94), the right-most column any spec reads
FIRST_DATA_ROW = HEADER_ROWS + 1    # 1-based sheet row of the first data row


def blank_row() -> list[_Cell]:
    return [_Cell() for _ in range(GRID_WIDTH)]


def put(row: list[_Cell], ref: str, text: str = "", date=None, time=None) -> None:
    """Write a cell by spreadsheet column letter."""
    row[_col(ref)] = _Cell(text=text, date=date, time=time)


def header_band(**overrides: str) -> list[list[_Cell]]:
    """A valid header: every required column labelled, in the band's second
    row — where the real template keeps most of them.

    `overrides` replace a column's label (pass "" to leave the column
    unlabelled), which is how the layout tests build a broken sheet.
    """
    labels = {**EXPECTED_HEADERS, **overrides}
    rows = [blank_row() for _ in range(HEADER_ROWS)]
    for column, label in labels.items():
        if label:
            put(rows[1], column, text=label)
    return rows


@pytest.fixture
def stub_open(monkeypatch):
    """Make slice_plan read a hand-built grid instead of a real file.

    A valid header band is prepended, so slicing tests can supply only the
    data rows they care about. Data therefore starts at sheet row
    FIRST_DATA_ROW.
    """
    def _install(rows: list[list[_Cell]]):
        monkeypatch.setattr(ingest, "_open", lambda path: _Grid([*header_band(), *rows]))
    return _install


# ---------------------------------------------------------------------------
# Column letters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,index", [
    ("A", 0), ("C", 2), ("Z", 25), ("AA", 26), ("AH", 33),
    ("AZ", 51), ("BS", 70), ("CB", 79), ("CQ", 94),
])
def test_col_letters_map_to_indices(ref, index):
    assert _col(ref) == index


# ---------------------------------------------------------------------------
# The media column — stricter than the text ones: it must hold a real link
# ---------------------------------------------------------------------------

def test_has_media_accepts_urls():
    assert has_media("https://cdn.publer.com/uploads/abc/clip.mp4")
    assert has_media("http://example.com/a.jpg")
    # A temporary link is still a link: judging it is the linter's job.
    assert has_media("https://app.publer.com/uploads/tmp/x.png")
    # Comma-separated lists are Publer's multi-media syntax.
    assert has_media("https://a.com/1.jpg, https://a.com/2.jpg")


def test_has_media_rejects_non_urls():
    assert not has_media("")
    assert not has_media("   ")
    assert not has_media("нет ссылки")
    assert not has_media("TODO")


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------

def test_facebook_posts_sliced_from_absolute_columns(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "C", time=(18, 30))
    put(r1, "E", text="Post body")
    put(r1, "G", text="https://cdn.publer.com/uploads/x/cover.jpg")
    put(r1, "H", text="#arcticdreams #2084")

    stub_open([r1])
    plans = slice_plan(tmp_path / "AUG2026.ods").files

    assert [p.name for p in plans] == ["AUG2026_FACEBOOK_OFFICIAL_POSTS"]
    plan = plans[0]
    assert plan.platform is Platform.FACEBOOK

    (row,) = plan.rows
    assert row.date == "2026/08/12 18:30"
    assert row.text == "Post body\n\n#arcticdreams #2084"
    assert row.media_url == "https://cdn.publer.com/uploads/x/cover.jpg"
    assert row.link == ""            # Publer import bug: Link must stay empty
    assert row.label == "FACEBOOK_OFFICIAL_POSTS"


def fb_post_row(day: int) -> list[_Cell]:
    """A FACEBOOK_OFFICIAL_POSTS row with nothing to complain about — every
    mandatory field filled, and hashtags too."""
    r = blank_row()
    put(r, "A", date=dt.date(2026, 8, day))
    put(r, "C", time=(10, 0))
    put(r, "E", text="Post body")
    put(r, "G", text="https://cdn.publer.com/a.jpg")
    put(r, "H", text="#arcticdreams")
    return r


@pytest.mark.parametrize("empty,field", [
    ("A", "date"),
    ("C", "time"),
    ("E", "text"),
    ("G", "media"),
])
def test_a_gap_in_a_mandatory_field_warns_without_dropping_the_row(
    stub_open, tmp_path, empty, field
):
    """The gap may be deliberate — a time left for Publer to schedule — so the
    row still converts, carrying whatever the plan holds."""
    r1 = fb_post_row(12)
    r1[_col(empty)] = _Cell()

    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    (plan,) = sliced.files
    assert len(plan.rows) == 1, field
    assert for_unit(sliced, "FACEBOOK_OFFICIAL_POSTS").columns == (empty,)


def test_what_is_filled_is_what_is_written(stub_open, tmp_path):
    """No invention: an absent time yields a bare date, not a made-up hour."""
    r1 = fb_post_row(12)
    r1[_col("C")] = _Cell()

    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.rows[0].date == "2026/08/12"


def test_a_row_with_no_date_at_all_writes_an_empty_date(stub_open, tmp_path):
    r1 = fb_post_row(12)
    r1[_col("A")] = _Cell()

    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.rows[0].date == ""
    assert plan.rows[0].media_url == "https://cdn.publer.com/a.jpg"


def test_optional_fields_are_not_required(stub_open, tmp_path):
    """Hashtags are optional: without them the row is still a post — it just
    earns a warning, because leaving them out is usually an oversight."""
    r1 = fb_post_row(12)
    r1[_col("H")] = _Cell()

    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    (plan,) = sliced.files
    assert plan.rows[0].text == "Post body"

    (warning,) = sliced.warnings
    assert warning.kind == "no_hashtags"
    assert warning.columns == ("H",)
    assert warning.message() == (
        f"FACEBOOK_OFFICIAL_POSTS, строка {FIRST_DATA_ROW}: не заполнено поле "
        "«Hashtags for the post» (H) — пост уйдёт без хэштегов. Пожалуйста, проверьте."
    )


def test_hashtag_cell_without_a_single_hash_warns(stub_open, tmp_path):
    """`arcticdreams metal` instead of `#arcticdreams #metal` — the tags
    would publish as plain words. Mechanical check: no `#` in the cell."""
    r1 = fb_post_row(12)
    put(r1, "H", text="arcticdreams metal")

    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    (warning,) = sliced.warnings
    assert warning.kind == "malformed_hashtags"
    assert warning.columns == ("H",)
    assert "нет ни одного" in warning.message()


def test_hashtag_cell_with_hashes_does_not_warn(stub_open, tmp_path):
    r1 = fb_post_row(12)   # H already holds "#arcticdreams"
    stub_open([r1])
    assert slice_plan(tmp_path / "PLAN.ods").warnings == []


def test_a_unit_with_no_hashtag_column_never_warns(stub_open, tmp_path):
    """TELEGRAMM has no hashtags column in the layout at all."""
    r1 = blank_row()
    put(r1, "CB", date=dt.date(2026, 8, 12))
    put(r1, "CD", time=(9, 0))
    put(r1, "CF", text="Пост на русском")
    put(r1, "CH", text="https://cdn.publer.com/tg.jpg")

    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")
    assert len(sliced.files) == 1
    assert sliced.warnings == []


def test_a_row_with_a_gap_is_not_also_nagged_about_hashtags(stub_open, tmp_path):
    """One warning per row: the gap is the thing to fix first."""
    r1 = fb_post_row(12)
    r1[_col("E")] = _Cell()      # no text
    r1[_col("H")] = _Cell()      # and no hashtags either

    stub_open([r1])
    (warning,) = slice_plan(tmp_path / "PLAN.ods").warnings
    assert warning.kind == "incomplete"
    assert warning.columns == ("E",)


def test_a_unit_started_but_never_finished_still_gets_its_file(stub_open, tmp_path):
    """FB clips with a media link and nothing else: the author may mean to add
    the time and description in Publer, so the file has to exist."""
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "L", text="https://cdn.publer.com/clip.mp4")

    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    (plan,) = sliced.files
    assert plan.unit == "FACEBOOK_OFFICIAL_CLIPS"
    (row,) = plan.rows
    assert row.media_url == "https://cdn.publer.com/clip.mp4"
    assert row.date == "2026/08/12"          # no time was given
    assert row.text == ""
    assert plan.unfinished == (FIRST_DATA_ROW,)


def test_a_partial_file_records_which_of_its_rows_have_gaps(stub_open, tmp_path):
    """The CSV looks finished on its own, so it has to carry the shortfall."""
    good_1, gappy, good_2 = fb_post_row(12), fb_post_row(13), fb_post_row(14)
    gappy[_col("E")] = _Cell()

    stub_open([good_1, gappy, good_2])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files

    assert len(plan.rows) == 3               # nothing is thrown away
    assert plan.unfinished == (FIRST_DATA_ROW + 1,)   # the sheet row, 1-based


def test_a_file_with_no_gaps_records_none(stub_open, tmp_path):
    stub_open([fb_post_row(12), fb_post_row(13)])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.unfinished == ()


def test_a_units_own_time_column_feeds_it(stub_open, tmp_path):
    """FB clips take J, their own time — not C, the post's time."""
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "K", text="Clip body")
    put(r1, "L", text="https://cdn.publer.com/clip.mp4")
    put(r1, "C", time=(11, 0))               # the POST's time is filled in…
    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.rows[0].date == "2026/08/12"   # …and the clip did not borrow it

    put(r1, "J", time=(19, 45))
    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.name == "PLAN_FACEBOOK_OFFICIAL_CLIPS"
    assert plan.rows[0].date == "2026/08/12 19:45"


def test_youtube_video_wants_its_title(stub_open, tmp_path):
    """Title is mandatory for the units that declare one — a gap warns."""
    r1 = blank_row()
    put(r1, "AZ", date=dt.date(2026, 8, 12))
    put(r1, "BG", time=(16, 0))
    put(r1, "BI", text="Video description")
    put(r1, "BJ", text="https://cdn.publer.com/video.mp4")
    stub_open([r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")
    assert for_unit(sliced, "YOUTUBE_VIDEO").columns == ("BH",)

    put(r1, "BH", text="Video headline")
    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.name == "PLAN_YOUTUBE_VIDEO"
    assert plan.rows[0].title == "Video headline"


def test_units_sharing_a_date_use_their_own_time_columns(stub_open, tmp_path):
    """FB posts (C) and FB clips (J) sit on one plan row but publish apart."""
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "C", time=(11, 0))
    put(r1, "E", text="Post")
    put(r1, "G", text="https://cdn.publer.com/post.jpg")
    put(r1, "J", time=(19, 45))
    put(r1, "K", text="Clip")
    put(r1, "L", text="https://cdn.publer.com/clip.mp4")

    stub_open([r1])
    plans = {p.name: p for p in slice_plan(tmp_path / "PLAN.ods").files}

    assert plans["PLAN_FACEBOOK_OFFICIAL_POSTS"].rows[0].date == "2026/08/12 11:00"
    assert plans["PLAN_FACEBOOK_OFFICIAL_CLIPS"].rows[0].date == "2026/08/12 19:45"


def test_instagram_people_tag_is_appended_to_text(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "O", date=dt.date(2026, 8, 12))
    put(r1, "Q", time=(12, 0))
    put(r1, "S", text="Caption")
    put(r1, "U", text="https://cdn.publer.com/ig.jpg")
    put(r1, "V", text="#2084")
    put(r1, "W", text="@someone")

    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files

    assert plan.platform is Platform.INSTAGRAM_BAND
    assert plan.rows[0].text == "Caption\n\n#2084\n\n@someone"


def test_instagram_reels_use_their_own_description(stub_open, tmp_path):
    """Reels read Z (описание рилз), never S (текст поста)."""
    r1 = blank_row()
    put(r1, "O", date=dt.date(2026, 8, 12))
    put(r1, "S", text="POST text, must not leak into the reel")
    put(r1, "X", time=(20, 15))
    put(r1, "Z", text="Reel description")
    put(r1, "AA", text="https://cdn.publer.com/reel.mp4")

    stub_open([r1])
    plan = file_for(slice_plan(tmp_path / "PLAN.ods"), "INSTAGRAMM_OFFICIAL_REELS")

    assert plan.name == "PLAN_INSTAGRAMM_OFFICIAL_REELS"
    assert plan.rows[0].text == "Reel description"


def test_instagram_exclusive_maps_to_the_funnel_account(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "AH", date=dt.date(2026, 8, 12))
    put(r1, "AJ", time=(8, 0))
    put(r1, "AL", text="Exclusive post")
    put(r1, "AN", text="https://cdn.publer.com/excl.jpg")

    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files

    assert plan.name == "PLAN_INSTAGRAMM_EXCLUSIVE_POSTS"
    assert plan.platform is Platform.INSTAGRAM_FUNNEL


def test_youtube_shorts_carry_a_title(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "AZ", date=dt.date(2026, 8, 12))
    put(r1, "BL", time=(17, 5))
    put(r1, "BM", text="Shorts headline")
    put(r1, "BN", text="Shorts description")
    put(r1, "BP", text="https://cdn.publer.com/short.mp4")
    put(r1, "BQ", text="#shorts")

    stub_open([r1])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files

    assert plan.name == "PLAN_YOUTUBE_SHORTS"
    assert plan.platform is Platform.YOUTUBE
    assert plan.rows[0].title == "Shorts headline"
    assert plan.rows[0].text == "Shorts description\n\n#shorts"


def test_tiktok_telegram_and_twitter_columns(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "BS", date=dt.date(2026, 8, 12))
    put(r1, "BU", time=(21, 0))
    put(r1, "BX", text="https://cdn.publer.com/tt.mp4")
    put(r1, "BY", text="TikTok caption")
    put(r1, "BZ", text="#tt")

    put(r1, "CB", date=dt.date(2026, 8, 12))
    put(r1, "CD", time=(7, 30))
    put(r1, "CF", text="Пост на русском")
    put(r1, "CH", text="https://cdn.publer.com/tg.jpg")

    put(r1, "CJ", date=dt.date(2026, 8, 12))
    put(r1, "CL", time=(15, 0))
    put(r1, "CN", text="Tweet")
    put(r1, "CP", text="https://cdn.publer.com/tw.jpg")
    put(r1, "CQ", text="#metal")

    stub_open([r1])
    plans = {p.name: p for p in slice_plan(tmp_path / "PLAN.ods").files}

    tt = plans["PLAN_TIKTOK"]
    assert tt.platform is Platform.TIKTOK
    assert tt.rows[0].text == "TikTok caption\n\n#tt"

    tg = plans["PLAN_TELEGRAMM"]
    assert tg.platform is Platform.TELEGRAM
    assert tg.rows[0].date == "2026/08/12 07:30"

    tw = plans["PLAN_TWITTER"]
    assert tw.platform is Platform.TWITTER
    assert tw.rows[0].text == "Tweet\n\n#metal"


# ---------------------------------------------------------------------------
# Layout validation — exact labels at exact addresses
# ---------------------------------------------------------------------------

def test_the_whole_layout_is_under_contract_not_just_mapped_columns():
    """Checking every labelled column is what makes a shifted sheet detectable
    — the columns the converter reads would often land on some other block's
    header and pass on their own."""
    from src.ingest import PLAN_SPECS
    mapped = {ref for spec in PLAN_SPECS for _, ref in spec.columns()}
    assert mapped <= set(EXPECTED_HEADERS)
    assert len(EXPECTED_HEADERS) > len(mapped)


def test_columns_are_checked_left_to_right():
    assert checked_columns() == sorted(EXPECTED_HEADERS, key=_col)
    assert checked_columns()[:3] == ["A", "B", "C"]


def test_units_reading_names_what_a_column_feeds():
    assert units_reading("A") == ("FACEBOOK_OFFICIAL_POSTS", "FACEBOOK_OFFICIAL_CLIPS")
    assert units_reading("BL") == ("YOUTUBE_SHORTS",)
    assert units_reading("B") == ()          # "Day" is in the layout, unread


def test_the_layouts_own_header_passes():
    assert check_layout(_Grid(header_band())) == []


def test_a_renamed_header_is_rejected():
    """Exact match: a translated layout is a different layout."""
    (bad,) = check_layout(_Grid(header_band(C="Время публикации")))

    assert bad.column == "C"
    assert bad.expected == "Post publication time"
    assert bad.found == "Время публикации"
    assert bad.message() == (
        "Колонка C: ожидается «Post publication time», найдено «Время публикации»"
    )


def test_a_missing_header_is_reported_as_absent():
    (bad,) = check_layout(_Grid(header_band(BL="")))

    assert bad.units == ("YOUTUBE_SHORTS",)
    assert bad.message() == "Колонка BL: отсутствует заголовок «Shorts publication time»"


def test_a_shifted_layout_fails_wholesale():
    """Every column moved one to the right: nothing lines up any more."""
    rows = [blank_row() for _ in range(HEADER_ROWS)]
    for column, label in EXPECTED_HEADERS.items():
        shifted = _col(column) + 1
        if shifted < GRID_WIDTH:
            rows[1][shifted] = _Cell(text=label)

    assert len(check_layout(_Grid(rows))) == len(EXPECTED_HEADERS)


def test_a_label_anywhere_in_the_header_band_counts():
    """The template keeps the YouTube post sub-headers a row below the rest."""
    rows = header_band(BD="")
    put(rows[HEADER_ROWS - 1], "BD", text=EXPECTED_HEADERS["BD"])
    assert check_layout(_Grid(rows)) == []


def test_a_label_below_the_header_band_does_not_count():
    rows = [*header_band(BD=""), blank_row()]
    put(rows[HEADER_ROWS], "BD", text=EXPECTED_HEADERS["BD"])
    assert [m.column for m in check_layout(_Grid(rows))] == ["BD"]


def test_a_header_merged_down_over_rows_needs_no_special_handling():
    """A vertical merge stores its text in the anchor — same column, still
    inside the band — so the scan finds it with no merge bookkeeping."""
    rows = header_band(H="")
    put(rows[0], "H", text=EXPECTED_HEADERS["H"])   # anchor of an H1:H3 merge
    assert check_layout(_Grid(rows)) == []


def test_a_block_title_is_not_quoted_back_as_the_columns_header():
    """Row 1 carries the block titles (FACEBOOK, YOUTUBE). Reporting one as
    "found" would send the reader to the wrong cell."""
    rows = header_band(A="Дата")
    put(rows[0], "A", text="FACEBOOK")

    (bad,) = check_layout(_Grid(rows))
    assert bad.found == "Дата"


def test_slice_plan_refuses_a_broken_layout(tmp_path, monkeypatch):
    rows = header_band(CL="")
    monkeypatch.setattr(ingest, "_open", lambda path: _Grid([*rows, fb_post_row(12)]))

    with pytest.raises(PlanLayoutError) as excinfo:
        slice_plan(tmp_path / "PLAN.ods")

    (bad,) = excinfo.value.mismatches
    assert bad.column == "CL"
    assert "необходимо поправить макет" in str(excinfo.value)


def test_all_mismatches_are_reported_at_once_left_to_right(tmp_path, monkeypatch):
    """One upload, one full list — not one error per re-upload."""
    monkeypatch.setattr(
        ingest, "_open", lambda path: _Grid(header_band(CN="", E="", BH=""))
    )

    with pytest.raises(PlanLayoutError) as excinfo:
        slice_plan(tmp_path / "PLAN.ods")
    assert [m.column for m in excinfo.value.mismatches] == ["E", "BH", "CN"]


# ---------------------------------------------------------------------------
# Unfinished rows — filled in part, not in whole
# ---------------------------------------------------------------------------

def file_for(sliced, unit: str):
    """The file produced for `unit`. Rows often start several units at once."""
    (plan,) = [f for f in sliced.files if f.unit == unit]
    return plan


def for_unit(sliced, unit: str):
    """The one warning raised for `unit`.

    A row usually raises several: the date column is shared by the units in a
    block, so filling it starts all of them at once.
    """
    (warning,) = [w for w in sliced.warnings if w.unit == unit]
    return warning


def test_an_unfinished_row_names_the_unit_row_and_empty_columns(stub_open, tmp_path):
    r1 = fb_post_row(12)
    r1[_col("G")] = _Cell()                  # media left empty

    stub_open([blank_row(), r1])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    assert len(file_for(sliced, "FACEBOOK_OFFICIAL_POSTS").rows) == 1
    warning = for_unit(sliced, "FACEBOOK_OFFICIAL_POSTS")
    assert warning.unit == "FACEBOOK_OFFICIAL_POSTS"
    # 1-based, as shown in the sheet: header band, one blank, then this row.
    assert warning.sheet_row == FIRST_DATA_ROW + 1
    assert warning.columns == ("G",)


def test_every_empty_mandatory_column_is_listed_left_to_right(stub_open, tmp_path):
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "E", text="Post body")           # started, but time and media absent

    stub_open([r1])
    warning = for_unit(slice_plan(tmp_path / "PLAN.ods"), "FACEBOOK_OFFICIAL_POSTS")
    assert warning.columns == ("C", "G")


def test_a_row_with_nothing_filled_is_not_a_warning(stub_open, tmp_path):
    """Nothing at all in the unit's columns means it wasn't planned that day."""
    stub_open([blank_row()])
    assert slice_plan(tmp_path / "PLAN.ods").warnings == []


def test_a_date_and_a_time_alone_are_not_a_start(stub_open, tmp_path):
    """Both are scaffolding: the date column belongs to the whole block, the
    time column is a formula dragged down every row. Neither says a unit was
    begun, so a row carrying only those must stay silent."""
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))
    put(r1, "C", time=(10, 0))
    put(r1, "J", time=(19, 45))
    put(r1, "O", date=dt.date(2026, 8, 12))
    put(r1, "Q", time=(12, 0))

    stub_open([r1])
    assert slice_plan(tmp_path / "PLAN.ods").warnings == []


def test_one_cell_of_content_does_start_a_unit(stub_open, tmp_path):
    """Text, media or a title is what counts — and only that unit is warned."""
    r1 = blank_row()
    put(r1, "A", date=dt.date(2026, 8, 12))   # shared by both Facebook units
    put(r1, "C", time=(10, 0))                # the post's time, filled by formula
    put(r1, "L", text="https://cdn.publer.com/clip.mp4")   # only the clip was begun

    stub_open([r1])
    (warning,) = slice_plan(tmp_path / "PLAN.ods").warnings
    assert warning.unit == "FACEBOOK_OFFICIAL_CLIPS"
    assert warning.columns == ("J", "K")


def test_complete_rows_are_never_warned_about(stub_open, tmp_path):
    stub_open([fb_post_row(12)])
    sliced = slice_plan(tmp_path / "PLAN.ods")
    assert len(sliced.files) == 1
    # FB clips share the date column but nothing of theirs was filled in.
    assert sliced.warnings == []


def test_warnings_are_raised_for_units_that_produced_a_file_too(stub_open, tmp_path):
    good, bad = fb_post_row(12), fb_post_row(13)
    bad[_col("C")] = _Cell()                 # this one loses its time

    stub_open([good, bad])
    sliced = slice_plan(tmp_path / "PLAN.ods")

    assert len(sliced.files) == 1 and len(sliced.files[0].rows) == 2
    assert for_unit(sliced, "FACEBOOK_OFFICIAL_POSTS").columns == ("C",)


def test_the_warning_reads_as_a_sentence(stub_open, tmp_path):
    r1 = fb_post_row(12)
    r1[_col("E")] = _Cell()

    stub_open([r1])
    warning = for_unit(slice_plan(tmp_path / "PLAN.ods"), "FACEBOOK_OFFICIAL_POSTS")
    assert warning.message() == (
        f"FACEBOOK_OFFICIAL_POSTS, строка {FIRST_DATA_ROW}: похоже, не заполнено "
        "поле «Post text» (E). Пожалуйста, проверьте."
    )


def test_plan_csv_carries_its_unit_suffix(stub_open, tmp_path):
    """The report groups skips by unit, so the file must name its unit."""
    stub_open([fb_post_row(12)])
    (plan,) = slice_plan(tmp_path / "PLAN.ods").files
    assert plan.unit == "FACEBOOK_OFFICIAL_POSTS"


def test_unsupported_extension_is_rejected(tmp_path):
    plan = tmp_path / "PLAN.numbers"
    plan.write_text("not a spreadsheet")
    with pytest.raises(ValueError, match="Unsupported content-plan format"):
        slice_plan(plan)


# ---------------------------------------------------------------------------
# .xls STRING records — xlrd miscounts non-BMP characters
# ---------------------------------------------------------------------------

class FakeBook:
    """Stands in for xlrd's Book: hands out the records that follow."""

    def __init__(self, *records):
        self.queue = list(records)
        self.encoding = "latin_1"

    def get_record_parts(self):
        if not self.queue:
            raise AssertionError("reader asked for a record it should not need")
        return self.queue.pop(0)


class FakeSheet:
    biff_version = 80

    def __init__(self, book):
        self.book = book


def string_record(text: str, *, declared: int = None) -> bytes:
    """A BIFF8 STRING record: length in UTF-16 code units, then the text."""
    import struct
    payload = text.encode("utf_16_le")
    units = declared if declared is not None else len(payload) // 2
    return struct.pack("<H", units) + b"\x01" + payload


def read_string(sheet, data):
    return ingest._xlrd_string_record_contents(sheet, data)


def test_a_string_record_holding_an_emoji_is_read_whole():
    """The bug: an emoji is one Python character but two UTF-16 code units, so
    xlrd read the record as unfinished and demanded a CONTINUE that no writer
    ever wrote — XLRDError on the whole file."""
    text = "Inside the making of Letargin. 🎧"
    sheet = FakeSheet(FakeBook())          # asking for another record fails

    assert read_string(sheet, string_record(text)) == text


def test_a_plain_string_record_is_unaffected():
    sheet = FakeSheet(FakeBook())
    assert read_string(sheet, string_record("Post text")) == "Post text"


def test_a_genuinely_split_string_still_follows_continue_records():
    """The fix must not break the case CONTINUE records exist for."""
    from xlrd.biffh import XL_CONTINUE

    head, tail = "Shorts description ", "continued in the next record"
    book = FakeBook((XL_CONTINUE, 0, b"\x01" + tail.encode("utf_16_le")))
    sheet = FakeSheet(book)

    data = string_record(head, declared=len((head + tail).encode("utf_16_le")) // 2)
    assert read_string(sheet, data) == head + tail


def test_a_truly_missing_continue_record_still_raises():
    """Tolerating a real gap would mean silently losing cells."""
    from xlrd.biffh import XLRDError

    book = FakeBook((0x00BE, 0, b""))      # MULBLANK, not CONTINUE
    sheet = FakeSheet(book)

    data = string_record("half", declared=99)
    with pytest.raises(XLRDError, match="Expected CONTINUE record"):
        read_string(sheet, data)


# ---------------------------------------------------------------------------
# .xlsx round-trip — the real openpyxl backend, no stub
# ---------------------------------------------------------------------------

def write_xlsx(path: Path, sheet_name: str = ingest.CONTENT_SHEET) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    # The layout's header band, so the file passes validation.
    for column, label in EXPECTED_HEADERS.items():
        ws[f"{column}2"] = label

    ws["A5"] = dt.datetime(2026, 8, 12)
    ws["C5"] = dt.time(18, 30)
    ws["E5"] = "Post body"
    ws["G5"] = "https://cdn.publer.com/uploads/x/cover.jpg"
    ws["H5"] = "#arcticdreams #2084"

    # A second unit on the same date, with its own time column.
    ws["J5"] = dt.time(21, 0)
    ws["K5"] = "Clip body"
    ws["L5"] = "https://cdn.publer.com/uploads/x/clip.mp4"

    # A row with text but no media -> must not become a post.
    ws["A6"] = dt.datetime(2026, 8, 13)
    ws["E6"] = "No media yet"

    wb.save(path)
    return path


def test_xlsx_is_read_end_to_end(tmp_path):
    plan = write_xlsx(tmp_path / "AUG2026.xlsx")
    sliced = slice_plan(plan)
    plans = {p.name: p for p in sliced.files}

    assert set(plans) == {"AUG2026_FACEBOOK_OFFICIAL_POSTS", "AUG2026_FACEBOOK_OFFICIAL_CLIPS"}

    posts = plans["AUG2026_FACEBOOK_OFFICIAL_POSTS"]
    assert posts.platform is Platform.FACEBOOK
    # The 2026/08/13 row has text and nothing else — it converts, with gaps,
    # and is flagged rather than dropped.
    assert [r.date for r in posts.rows] == ["2026/08/12 18:30", "2026/08/13"]
    assert posts.rows[0].text == "Post body\n\n#arcticdreams #2084"
    assert posts.rows[0].media_url == "https://cdn.publer.com/uploads/x/cover.jpg"
    assert posts.unfinished == (6,)

    clips = plans["AUG2026_FACEBOOK_OFFICIAL_CLIPS"]
    assert [r.date for r in clips.rows] == ["2026/08/12 21:00"]


def test_xlsx_does_not_keep_the_file_locked(tmp_path):
    """Uploads are read from a temp copy the caller then deletes — on Windows
    that fails if openpyxl's read-only handle is still open."""
    plan = write_xlsx(tmp_path / "PLAN.xlsx")
    slice_plan(plan)
    plan.unlink()          # raises PermissionError on Windows if still open
    assert not plan.exists()


def test_xlsx_falls_back_to_the_first_sheet(tmp_path):
    """Plans exported from other tools may not name the sheet Контент-план."""
    plan = write_xlsx(tmp_path / "PLAN.xlsx", sheet_name="Sheet1")
    plans = slice_plan(plan).files
    assert plans and plans[0].rows


def test_source_name_overrides_the_temp_path(stub_open, tmp_path):
    """Uploads land in a random temp file; the plan's real name must win."""
    stub_open([fb_post_row(12)])
    (plan,) = slice_plan(tmp_path / "tmpq7x3k1.ods", source_name="Контент-план АВГУСТ.ods").files
    assert plan.name.endswith("_FACEBOOK_OFFICIAL_POSTS")
    assert not plan.name.startswith("tmp")
