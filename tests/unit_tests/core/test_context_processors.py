from datetime import UTC, datetime

from core.context_processors import build_info

FULL_SHA = "5e4f0d09441f31cf8fc6e59b3cc316ecad1205b5"


def test_build_info_without_stamp(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", "")
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "")

    info = build_info(None)["build_info"]

    assert info["sha"] is None
    assert info["sha_short"] is None
    assert info["build_date"] is None
    assert info["commit_url"] is None


def test_build_info_with_stamp(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", FULL_SHA)
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "2026-08-12T10:00:00.000Z")

    info = build_info(None)["build_info"]

    assert info["sha"] == FULL_SHA
    assert info["sha_short"] == "5e4f0d0"
    assert info["build_date"] == datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    assert info["commit_url"] == f"https://github.com/srtab/daiv/commit/{FULL_SHA}"


def test_build_info_with_malformed_build_date(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", FULL_SHA)
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "not-a-date")

    info = build_info(None)["build_info"]

    assert info["build_date"] is None
    assert info["sha_short"] == "5e4f0d0"
