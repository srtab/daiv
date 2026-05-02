from django.test import RequestFactory

from core.htmx import is_htmx


def test_is_htmx_true_when_header_set():
    rf = RequestFactory()
    req = rf.get("/", headers={"HX-Request": "true"})
    assert is_htmx(req) is True


def test_is_htmx_false_when_header_absent():
    rf = RequestFactory()
    req = rf.get("/")
    assert is_htmx(req) is False


def test_is_htmx_false_when_header_value_other():
    rf = RequestFactory()
    req = rf.get("/", headers={"HX-Request": "no"})
    assert is_htmx(req) is False
