from django.template import Context, Template
from django.test import override_settings


def render(tpl: str, **ctx) -> str:
    return Template("{% load vite %}" + tpl).render(Context(ctx))


@override_settings(VITE_DEV_SERVER="http://localhost:5173")
def test_vite_asset_in_dev_mode_emits_module_script_pointing_at_dev_server():
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert '<script type="module" src="http://localhost:5173/@vite/client"></script>' in out
    assert '<script type="module" src="http://localhost:5173/src/main.tsx"></script>' in out


@override_settings(VITE_DEV_SERVER=None)
def test_vite_asset_in_prod_mode_reads_manifest_and_emits_hashed_bundle(tmp_path, settings):
    settings.STATICFILES_DIRS = [str(tmp_path)]
    manifest_dir = tmp_path / "chat" / "dist"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        '{"src/main.tsx": {"file": "assets/main-abc123.js", "css": ["assets/main-abc123.css"]}}'
    )
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert "assets/main-abc123.js" in out
    assert "assets/main-abc123.css" in out
    assert "@vite/client" not in out


@override_settings(VITE_DEV_SERVER=None)
def test_vite_asset_returns_html_comment_when_manifest_missing(tmp_path, settings):
    settings.STATICFILES_DIRS = [str(tmp_path)]
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert "<script" not in out
    assert "vite_asset: failed to load entry" in out


@override_settings(VITE_DEV_SERVER=None)
def test_vite_asset_returns_html_comment_when_entry_not_in_manifest(tmp_path, settings):
    settings.STATICFILES_DIRS = [str(tmp_path)]
    manifest_dir = tmp_path / "chat" / "dist"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text('{"src/other.tsx": {"file": "x.js"}}')
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert "<script" not in out
    assert "vite_asset: failed to load entry" in out


@override_settings(VITE_DEV_SERVER=None)
def test_vite_asset_returns_html_comment_when_manifest_invalid_json(tmp_path, settings):
    settings.STATICFILES_DIRS = [str(tmp_path)]
    manifest_dir = tmp_path / "chat" / "dist"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text("not json {")
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert "<script" not in out
    assert "vite_asset: failed to load entry" in out


@override_settings(VITE_DEV_SERVER=None)
def test_vite_asset_in_prod_omits_css_link_when_no_css(tmp_path, settings):
    settings.STATICFILES_DIRS = [str(tmp_path)]
    manifest_dir = tmp_path / "chat" / "dist"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text('{"src/main.tsx": {"file": "assets/main.js"}}')
    out = render("{% vite_asset 'src/main.tsx' %}")
    assert "assets/main.js" in out
    assert "<link" not in out
