from datetime import UTC, datetime

from core.context_processors import _BUILD_INFO, _compute_build_info, build_info

FULL_SHA = "5e4f0d09441f31cf8fc6e59b3cc316ecad1205b5"


def test_build_info_returns_precomputed_constant():
    assert build_info(None) == {"build_info": _BUILD_INFO}


def test_compute_without_stamp(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", "")
    monkeypatch.setattr("core.context_processors.GIT_SHA_SHORT", "")
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "")

    info = _compute_build_info()

    assert info["sha"] is None
    assert info["sha_short"] is None
    assert info["build_date"] is None
    assert info["commit_url"] is None


def test_compute_with_stamp(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", FULL_SHA)
    monkeypatch.setattr("core.context_processors.GIT_SHA_SHORT", FULL_SHA[:7])
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "2026-08-12T10:00:00.000Z")
    monkeypatch.setattr("core.context_processors.REPO_URL", "https://github.com/acme/daiv-fork")

    info = _compute_build_info()

    assert info["sha"] == FULL_SHA
    assert info["sha_short"] == "5e4f0d0"
    assert info["build_date"] == datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    assert info["commit_url"] == f"https://github.com/acme/daiv-fork/commit/{FULL_SHA}"


def test_compute_with_malformed_build_date(monkeypatch):
    monkeypatch.setattr("core.context_processors.GIT_SHA", FULL_SHA)
    monkeypatch.setattr("core.context_processors.GIT_SHA_SHORT", FULL_SHA[:7])
    monkeypatch.setattr("core.context_processors.BUILD_DATE", "not-a-date")

    info = _compute_build_info()

    assert info["build_date"] is None
    assert info["sha_short"] == "5e4f0d0"
