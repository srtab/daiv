from django.apps import apps


def test_notifications_app_is_installed():
    assert apps.is_installed("notifications")
    assert apps.get_app_config("notifications").name == "notifications"
