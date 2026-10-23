from django.template.loader import render_to_string
from django.test import RequestFactory


def test_filter_pills_swap_their_own_param_and_drop_the_page():
    html = render_to_string(
        "memory/_filter_pills.html",
        {"label": "Status", "param": "status", "choices": [("active", "Active")], "current": ""},
        request=RequestFactory().get("/x/?category=pitfall&status=pending&page=3"),
    )

    assert 'href="?category=pitfall"' in html
    assert 'href="?category=pitfall&amp;status=active"' in html
