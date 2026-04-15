from django.template import Context, Template


def _render(template_source: str, context_dict: dict) -> str:
    return Template(template_source).render(Context(context_dict))


class TestNavActive:
    def test_returns_active_classes_when_section_matches(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", ctx)
        assert "bg-white/[0.06]" in out
        assert "text-white" in out

    def test_returns_empty_string_when_section_does_not_match(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'schedules' %}", ctx)
        assert out.strip() == ""

    def test_returns_empty_string_when_no_active_section_in_context(self):
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", {})
        assert out.strip() == ""
