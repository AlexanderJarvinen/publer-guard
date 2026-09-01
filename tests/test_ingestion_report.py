"""
Tests for the ingestion section of the run report.

`_ingestion_report` is a pure aggregation over what the slicer flagged, so it
is tested directly — no Flask request, no pipeline, no API calls.
"""

from src.app import _ingestion_report
from src.ingest import PLAN_SPECS, IncompleteRow, MissingHashtags, PlanCsv, PlanSlices
from src.state import Platform


def unfinished(unit: str, row: int, *columns: str) -> IncompleteRow:
    return IncompleteRow(unit=unit, sheet_row=row, columns=tuple(columns))


def no_hashtags(unit: str, row: int, column: str) -> MissingHashtags:
    return MissingHashtags(unit=unit, sheet_row=row, columns=(column,))


def test_nothing_wrong_reports_zero():
    report = _ingestion_report(PlanSlices())
    assert report["warnings"] == 0
    assert report["incomplete_rows"] == 0
    assert report["posts_without_hashtags"] == 0
    assert report["malformed_hashtags"] == 0
    assert report["by_unit"] == []


def test_rows_are_counted_per_unit_and_per_empty_column():
    sliced = PlanSlices(warnings=[
        unfinished("TIKTOK", 5, "BX"),
        unfinished("TIKTOK", 6, "BX"),
        unfinished("TIKTOK", 7, "BU"),
    ])
    report = _ingestion_report(sliced)

    assert report["warnings"] == 3
    (unit,) = report["by_unit"]
    assert unit["unit"] == "TIKTOK"
    assert unit["count"] == 3
    # Counted under the header the user sees in the sheet, not the field name.
    assert unit["missing"] == {
        "«Media link for the video» (BX)": 2,
        "«Post publication time TikTok» (BU)": 1,
    }


def test_the_two_kinds_are_counted_apart():
    """One blocks a post from existing, the other doesn't — the summary line
    has to say which is which."""
    sliced = PlanSlices(warnings=[
        unfinished("TIKTOK", 5, "BY"),
        no_hashtags("TWITTER", 5, "CQ"),
        no_hashtags("TWITTER", 6, "CQ"),
    ])
    report = _ingestion_report(sliced)

    assert report["warnings"] == 3
    assert report["incomplete_rows"] == 1
    assert report["posts_without_hashtags"] == 2
    assert [r["kind"] for r in report["rows"]] == ["incomplete", "no_hashtags", "no_hashtags"]


def test_busiest_unit_comes_first():
    sliced = PlanSlices(warnings=[
        unfinished("TWITTER", 5, "CP"),
        unfinished("TIKTOK", 5, "BX"),
        unfinished("TIKTOK", 6, "BX"),
    ])
    assert [u["unit"] for u in _ingestion_report(sliced)["by_unit"]] == ["TIKTOK", "TWITTER"]


def test_units_without_file_come_from_the_specs_not_the_warnings():
    """A unit can produce no file having raised no warning at all — nothing
    was filled in for it anywhere — and that still needs saying."""
    sliced = PlanSlices(
        files=[PlanCsv(name="PLAN_TIKTOK", platform=Platform.TIKTOK, unit="TIKTOK")],
    )
    without = _ingestion_report(sliced)["units_without_file"]

    assert "TIKTOK" not in without
    assert len(without) == len(PLAN_SPECS) - 1
    assert without == [s.suffix for s in PLAN_SPECS if s.suffix != "TIKTOK"]


def test_every_row_is_returned_however_many():
    """The UI pages through them; the server never truncates."""
    rows = [unfinished("TIKTOK", i, "BX") for i in range(500)]
    report = _ingestion_report(PlanSlices(warnings=rows))

    assert report["warnings"] == 500
    assert len(report["rows"]) == 500


def test_row_detail_carries_the_sheet_row_columns_and_message():
    sliced = PlanSlices(warnings=[unfinished("YOUTUBE_VIDEO", 12, "BH", "BJ")])
    (row,) = _ingestion_report(sliced)["rows"]

    assert row["unit"] == "YOUTUBE_VIDEO"
    assert row["sheet_row"] == 12
    assert row["columns"] == ["BH", "BJ"]
    assert row["message"] == (
        "YOUTUBE_VIDEO, строка 12: похоже, не заполнены поля "
        "«Video header» (BH), «Media link for the Video» (BJ). Пожалуйста, проверьте."
    )


def test_a_single_empty_field_reads_as_singular():
    (row,) = _ingestion_report(PlanSlices(warnings=[unfinished("TWITTER", 7, "CN")]))["rows"]
    assert row["message"] == (
        "TWITTER, строка 7: похоже, не заполнено поле «Tweet text» (CN). "
        "Пожалуйста, проверьте."
    )


def test_the_hashtag_warning_says_the_post_still_goes_out():
    (row,) = _ingestion_report(PlanSlices(warnings=[no_hashtags("TIKTOK", 9, "BZ")]))["rows"]
    assert row["message"] == (
        "TIKTOK, строка 9: не заполнено поле «Hashtags» (BZ) — пост уйдёт без "
        "хэштегов. Пожалуйста, проверьте."
    )
