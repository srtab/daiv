from django.urls import reverse

from daiv import __version__


def test_health_check_returns_200(client):
    """Test that the health check endpoint returns 200 OK with correct content."""
    response = client.get(reverse("health_check"))

    assert response.status_code == 200
    assert response.content.decode("utf-8") == "OK"
    assert response["Content-Type"] == "text/plain"


def test_version_without_build_stamp(client, monkeypatch):
    monkeypatch.setattr("core.views.GIT_SHA", "")
    monkeypatch.setattr("core.views.BUILD_DATE", "")

    response = client.get(reverse("version"))

    assert response.status_code == 200
    assert response.json() == {"version": __version__, "sha": None, "build_date": None}


def test_version_reports_stamped_build(client, monkeypatch):
    monkeypatch.setattr("core.views.GIT_SHA", "5e4f0d09441f31cf8fc6e59b3cc316ecad1205b5")
    monkeypatch.setattr("core.views.BUILD_DATE", "2026-08-12T10:00:00.000Z")

    data = client.get(reverse("version")).json()

    assert data["version"] == __version__
    assert data["sha"] == "5e4f0d09441f31cf8fc6e59b3cc316ecad1205b5"
    assert data["build_date"] == "2026-08-12T10:00:00.000Z"
