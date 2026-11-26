from django.test import Client

import pytest
from dashboard import landing
from dashboard.comparisons import COMPARISONS

from tests.unit_tests.blog.conftest import posts_dir, write_post  # noqa: F401

pytestmark = pytest.mark.django_db


def _llms_txt() -> str:
    response = Client(HTTP_HOST="daiv.internal.example").get("/llms.txt")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    return response.content.decode()


def test_opens_with_the_definition(posts_dir):  # noqa: F811
    assert _llms_txt().startswith(f"# DAIV\n\n> {landing.DEFINITION}\n")


def test_answers_every_faq_with_markdown_links(posts_dir):  # noqa: F811
    content = _llms_txt()

    for faq in landing.FAQS:
        assert f"**{faq.question}**" in content
    assert "<a " not in content
    assert f"[Quickstart]({landing.QUICKSTART_URL})" in content


def test_links_every_comparison_and_published_post_at_daivagent_com(write_post):  # noqa: F811
    write_post()
    write_post(slug="unpublished", draft=True)

    content = _llms_txt()

    for comparison in COMPARISONS:
        assert f"](https://daivagent.com/compare/{comparison.slug}/): {comparison.summary}" in content
    assert "- [Egress](https://daivagent.com/blog/egress/): How egress works." in content
    assert "/blog/unpublished/" not in content
