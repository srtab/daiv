from django.urls import reverse


def test_health_check_returns_200(client):
    """Test that the health check endpoint returns 200 OK with correct content."""
    response = client.get(reverse("health_check"))

    assert response.status_code == 200
    assert response.content.decode("utf-8") == "OK"
    assert response["Content-Type"] == "text/plain"
