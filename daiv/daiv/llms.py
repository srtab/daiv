"""The site's `/llms.txt`: the homepage's definition and FAQ, and every public page, in Markdown for answer engines.

It sits here rather than in `dashboard` because it lists blog posts, and `blog` already imports `dashboard`.
"""

from django.shortcuts import render

from blog import posts
from dashboard import landing
from dashboard.comparisons import COMPARISONS

from daiv import CANONICAL_ORIGIN, DOCS_URL


def llms_txt(request):
    return render(
        request,
        "dashboard/llms.txt",
        {
            "origin": CANONICAL_ORIGIN,
            "docs_url": DOCS_URL,
            "quickstart_url": landing.QUICKSTART_URL,
            "deployment_url": landing.DEPLOYMENT_URL,
            "definition": landing.DEFINITION,
            "faqs": landing.FAQS,
            "comparisons": COMPARISONS,
            "posts": posts.visible_posts(),
        },
        content_type="text/plain; charset=utf-8",
    )
