"""
Tests for the public-deploy guards in src/app.py: the daily run quota and
the upload size cap. No pipeline, no API calls — the guards reject before
any real work starts.
"""

import datetime as dt
import io

import pytest

from src import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture
def limited(monkeypatch):
    """Set a limit of 2 runs/day and reset the counter."""
    monkeypatch.setattr(app_mod, "_RUN_LIMIT_PER_DAY", 2)
    monkeypatch.setattr(
        app_mod, "_run_counter", app_mod._RunQuota(date=dt.date.today(), count=0)
    )


# ── Daily run quota ──────────────────────────────────────────────────────────

def test_unlimited_by_default(monkeypatch):
    monkeypatch.setattr(app_mod, "_RUN_LIMIT_PER_DAY", 0)
    assert app_mod._quota_exhausted() is None


def test_quota_allows_up_to_the_limit(limited):
    assert app_mod._quota_exhausted() is None
    app_mod._count_run()
    assert app_mod._quota_exhausted() is None
    app_mod._count_run()
    assert "Daily demo limit reached" in app_mod._quota_exhausted()


def test_quota_resets_on_a_new_day(limited):
    app_mod._run_counter.date = dt.date.today() - dt.timedelta(days=1)
    app_mod._run_counter.count = 99
    assert app_mod._quota_exhausted() is None
    assert app_mod._run_counter.count == 0


def test_exhausted_quota_returns_429_from_both_run_routes(limited, client):
    app_mod._run_counter.count = 2
    for route in ("/run", "/run-plan"):
        resp = client.post(route, data={})
        assert resp.status_code == 429, route
        assert "Daily demo limit" in resp.get_json()["error"]


def test_invalid_request_does_not_burn_quota(limited, client):
    resp = client.post("/run", data={})   # no files — fails validation
    assert resp.status_code == 400
    assert app_mod._run_counter.count == 0


# ── Upload size cap ──────────────────────────────────────────────────────────

def test_oversized_upload_is_rejected_with_a_json_413(client, monkeypatch):
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", 1024)
    resp = client.post(
        "/run-plan",
        data={"file": (io.BytesIO(b"x" * 4096), "plan.xls")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413
    assert "too large" in resp.get_json()["error"]


def test_max_content_length_is_configured():
    assert app_mod.app.config["MAX_CONTENT_LENGTH"] == 16 * 1024 * 1024
