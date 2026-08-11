"""
Tests for the ingestion section of the run report.

`_ingestion_report` is a pure aggregation over what the slicer dropped, so it
is tested directly — no Flask request, no pipeline, no API calls.
"""

from src.app import _ingestion_report
from src.ingest import PlanCsv, PlanSlices, SkippedRow
from src.state import Platform


def skipped(unit: str, row: int, *missing: str) -> SkippedRow:
    return SkippedRow(unit=unit, sheet_row=row, missing=tuple(missing))


def test_nothing_skipped_reports_zero():
    report = _ingestion_report(PlanSlices())
    assert report["skipped_rows"] == 0
    assert report["by_unit"] == []
    assert report["units_without_file"] == []


def test_skips_are_counted_per_unit_and_per_missing_column():
    sliced = PlanSlices(skipped=[
        skipped("TIKTOK", 5, "media (BX)"),
        skipped("TIKTOK", 6, "media (BX)"),
        skipped("TIKTOK", 7, "time (BU)"),
    ])
    report = _ingestion_report(sliced)

    assert report["skipped_rows"] == 3
    (unit,) = report["by_unit"]
    assert unit["unit"] == "TIKTOK"
    assert unit["count"] == 3
    assert unit["missing"] == {"media (BX)": 2, "time (BU)": 1}


def test_busiest_unit_comes_first():
    sliced = PlanSlices(skipped=[
        skipped("TWITTER", 5, "media (CP)"),
        skipped("TIKTOK", 5, "media (BX)"),
        skipped("TIKTOK", 6, "media (BX)"),
    ])
    assert [u["unit"] for u in _ingestion_report(sliced)["by_unit"]] == ["TIKTOK", "TWITTER"]


def test_units_without_file_explain_an_absent_output():
    """A unit that produced a file is not "missing" — only a barren one is."""
    sliced = PlanSlices(
        files=[PlanCsv(name="PLAN_TIKTOK", platform=Platform.TIKTOK, unit="TIKTOK")],
        skipped=[
            skipped("TIKTOK", 5, "media (BX)"),     # partial, but the file exists
            skipped("TWITTER", 5, "media (CP)"),    # nothing survived here
        ],
    )
    assert _ingestion_report(sliced)["units_without_file"] == ["TWITTER"]


def test_row_detail_is_capped_but_the_count_is_not():
    from src.app import _MAX_SKIPPED_DETAIL

    over = _MAX_SKIPPED_DETAIL + 25
    sliced = PlanSlices(skipped=[skipped("TIKTOK", i, "media (BX)") for i in range(over)])
    report = _ingestion_report(sliced)

    assert report["skipped_rows"] == over               # the counter stays honest
    assert len(report["rows"]) == _MAX_SKIPPED_DETAIL   # the payload stays sane
    assert report["rows_truncated"] is True


def test_row_detail_carries_the_sheet_row_and_columns():
    sliced = PlanSlices(skipped=[skipped("YOUTUBE_VIDEO", 12, "title (BH)", "media (BJ)")])
    (row,) = _ingestion_report(sliced)["rows"]

    assert row == {
        "unit": "YOUTUBE_VIDEO",
        "sheet_row": 12,
        "missing": ["title (BH)", "media (BJ)"],
    }
