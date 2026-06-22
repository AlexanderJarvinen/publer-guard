"""
Tests for src/tools.py — lookup_media, all branches.

Tests against the real fixture use the actual file on disk so they validate
integration with the fixture data as it exists. Tests that need controlled or
unusual data build a temporary fixture via pytest's tmp_path.
"""

import json
import pytest
from pathlib import Path

from src.tools import lookup_media

# ---------------------------------------------------------------------------
# Known values from fixtures/media_library.json
# ---------------------------------------------------------------------------

_CDN_2084 = (
    "https://cdn.publer.com/uploads/videos/"
    "6a3711e930d9b2bc52422eff/"
    "84791791ca0c92f1acea4d46a9ead09b.mp4"
)
_CDN_LETARGIN = (
    "https://cdn.publer.com/uploads/videos/"
    "6a2b5c401b935b23ddb5bc7a/"
    "3d7dbbf28c953b558b3c7df085d90a36.mov"
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_fixture(tmp_path: Path, entries: list[dict]) -> Path:
    f = tmp_path / "media_library.json"
    f.write_text(json.dumps({"media": entries}), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Happy-path — real fixture
# ---------------------------------------------------------------------------

class TestRealFixture:
    def test_exact_name_match_returns_permanent_url(self):
        assert lookup_media("2084_part2_room101.mp4") == _CDN_2084

    def test_partial_name_match(self):
        assert lookup_media("letargin") == _CDN_LETARGIN

    def test_hash_fragment_from_path_finds_entry(self):
        # The hash appears inside the path, not in the name
        assert lookup_media("84791791ca0c92f1acea4d46a9ead09b") == _CDN_2084

    def test_id_fragment_from_path_finds_entry(self):
        assert lookup_media("6a2b5c401b935b23ddb5bc7a") == _CDN_LETARGIN

    def test_case_insensitive_name_match(self):
        assert lookup_media("2084_PART2_ROOM101.MP4") == _CDN_2084

    def test_case_insensitive_mixed(self):
        assert lookup_media("LeTaRgIn") == _CDN_LETARGIN

    def test_no_match_returns_none(self):
        assert lookup_media("nonexistent_video_xyz.mp4") is None

    def test_empty_hint_returns_none(self):
        # Empty hint matches every entry → ambiguous → None
        assert lookup_media("") is None


# ---------------------------------------------------------------------------
# Ambiguity — more than one match → None (do not guess)
# ---------------------------------------------------------------------------

class TestAmbiguity:
    def test_hint_matching_multiple_entries_returns_none(self, tmp_path):
        f = _make_fixture(tmp_path, [
            {
                "name": "video_alpha.mp4",
                "path": "https://cdn.publer.com/uploads/videos/aaa/alpha.mp4",
            },
            {
                "name": "video_beta.mp4",
                "path": "https://cdn.publer.com/uploads/videos/bbb/beta.mp4",
            },
        ])
        # "video" is in both names — ambiguous
        assert lookup_media("video", _fixture_path=f) is None

    def test_hint_matching_via_path_and_name_is_still_ambiguous(self, tmp_path):
        f = _make_fixture(tmp_path, [
            {
                "name": "shared_hash_a.mp4",
                "path": "https://cdn.publer.com/uploads/videos/shared/a.mp4",
            },
            {
                "name": "other.mp4",
                "path": "https://cdn.publer.com/uploads/videos/shared/b.mp4",
            },
        ])
        # "shared" appears in both paths
        assert lookup_media("shared", _fixture_path=f) is None


# ---------------------------------------------------------------------------
# Safety gate — tmp URL or non-CDN path in fixture data
# ---------------------------------------------------------------------------

class TestSafetyGate:
    def test_tmp_url_in_fixture_returns_none(self, tmp_path):
        f = _make_fixture(tmp_path, [
            {
                "name": "bad_temp.mp4",
                "path": "https://app.publer.com/uploads/tmp/12345/bad.mp4",
            },
        ])
        assert lookup_media("bad_temp", _fixture_path=f) is None

    def test_non_cdn_url_returns_none(self, tmp_path):
        f = _make_fixture(tmp_path, [
            {
                "name": "external.mp4",
                "path": "https://s3.amazonaws.com/bucket/external.mp4",
            },
        ])
        assert lookup_media("external", _fixture_path=f) is None

    def test_empty_path_returns_none(self, tmp_path):
        f = _make_fixture(tmp_path, [{"name": "nameless.mp4", "path": ""}])
        assert lookup_media("nameless", _fixture_path=f) is None


# ---------------------------------------------------------------------------
# Zero entries
# ---------------------------------------------------------------------------

class TestZeroEntries:
    def test_empty_library_returns_none(self, tmp_path):
        f = _make_fixture(tmp_path, [])
        assert lookup_media("anything", _fixture_path=f) is None


# ---------------------------------------------------------------------------
# Error handling — bad fixture file
# ---------------------------------------------------------------------------

class TestFixtureErrors:
    def test_missing_fixture_raises_file_not_found(self, tmp_path):
        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError, match="fixture not found"):
            lookup_media("anything", _fixture_path=missing)

    def test_malformed_json_raises_value_error(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not: valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="malformed"):
            lookup_media("anything", _fixture_path=f)

    def test_missing_media_key_raises_value_error(self, tmp_path):
        f = tmp_path / "no_media.json"
        f.write_text(json.dumps({"other_key": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="malformed"):
            lookup_media("anything", _fixture_path=f)
