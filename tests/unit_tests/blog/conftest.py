import json
from datetime import date

import pytest
import yaml
from blog import posts

TODAY = date(2026, 12, 15)


@pytest.fixture
def posts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(posts, "POSTS_DIR", tmp_path)
    monkeypatch.setattr(posts, "today", lambda: TODAY)
    posts.all_posts.cache_clear()
    yield tmp_path
    posts.all_posts.cache_clear()


@pytest.fixture
def write_post(posts_dir):
    def write(slug="egress", body="Hello.", **front_matter):
        meta = {
            "title": "Egress",
            "date": date(2026, 12, 14),
            "summary": "How egress works.",
            "author": "Sandro Rodrigues",
            **front_matter,
        }
        (posts_dir / f"{slug}.md").write_text(f"---\n{yaml.safe_dump(meta)}---\n{body}\n")
        posts.all_posts.cache_clear()

    return write


@pytest.fixture
def manifest_storage(settings, tmp_path_factory):
    root = tmp_path_factory.mktemp("static")
    paths = {"blog/egress/img/flow.png": "blog/egress/img/flow.abc123.png"}
    (root / "staticfiles.json").write_text(json.dumps({"version": "1.1", "hash": "", "paths": paths}))
    settings.STATIC_ROOT = str(root)
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
    }
