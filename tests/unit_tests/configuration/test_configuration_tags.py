from django import forms

from configuration.templatetags.configuration_tags import get_field


class SimpleTestForm(forms.Form):
    title = forms.CharField(max_length=100)
    enabled = forms.BooleanField(required=False)


def test_get_field_returns_bound_field_for_known_name():
    form = SimpleTestForm()
    bound_title = get_field(form, "title")
    assert bound_title is not None
    assert bound_title.name == "title"

    bound_enabled = get_field(form, "enabled")
    assert bound_enabled is not None
    assert bound_enabled.name == "enabled"


def test_get_field_returns_none_for_unknown_name():
    form = SimpleTestForm()
    assert get_field(form, "nonexistent") is None
    assert get_field(form, "") is None
