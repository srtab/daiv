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
