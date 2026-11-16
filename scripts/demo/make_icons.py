"""Render the favicons, app icons and social avatar from the brand SVGs.

    uv run --no-project --with playwright==1.62.0 --with pillow==11.3.0 python -I scripts/demo/make_icons.py \
      --avatar ~/.cache/daiv-demo/launch/avatar.png

The rounded tile in `daiv/public/favicon.svg` makes the favicons. Apple and the web manifest round or mask icons
themselves, and social sites crop avatars to a circle, so those get a full-bleed square with the mark inside the
centre circle, drawn from `core/img/icons/daiv-mark.svg`.
"""

import argparse
import asyncio
import base64
import io
import re
from pathlib import Path

from PIL import Image
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "daiv" / "public"
FAVICON = PUBLIC / "favicon.svg"
MARK = ROOT / "daiv" / "core" / "static" / "core" / "img" / "icons" / "daiv-mark.svg"
DOCS_FAVICON = ROOT / "docs" / "assets" / "favicon.png"

VIOLET = "#8B5CF6"
MARK_SHARE = 0.52
ICO_SIZES = [16, 32, 48]
APP_ICONS = [("apple-touch-icon", 180), ("web-app-manifest-192x192", 192), ("web-app-manifest-512x512", 512)]


def data_uri(path: Path) -> str:
    return f"data:image/svg+xml;base64,{base64.b64encode(path.read_bytes()).decode()}"


def rounded(size: int) -> str:
    return f'<img src="{data_uri(FAVICON)}" width="{size}" height="{size}" style="display:block">'


def square(size: int) -> str:
    mask = f"url({data_uri(MARK)}) no-repeat center / contain"
    _, _, width, height = re.search(r'viewBox="([^"]+)"', MARK.read_text()).group(1).split()
    return (
        f'<div style="width:{size}px;height:{size}px;background:{VIOLET};display:grid;place-items:center">'
        f'<div style="height:{MARK_SHARE * 100}%;aspect-ratio:{width}/{height};background:#fff;'
        f'-webkit-mask:{mask};mask:{mask}"></div></div>'
    )


async def shot(tab: Page, html: str, size: int) -> Image.Image:
    await tab.set_viewport_size({"width": size, "height": size})
    await tab.set_content(f'<body style="margin:0;background:transparent">{html}</body>')
    return Image.open(io.BytesIO(await tab.screenshot(omit_background=True))).convert("RGBA")


async def render(args: argparse.Namespace) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, args=["--headless=new"])
        tab = await browser.new_page()

        ico = await shot(tab, rounded(256), 256)
        ico.save(PUBLIC / "favicon.ico", sizes=[(s, s) for s in ICO_SIZES])
        small = await shot(tab, rounded(96), 96)
        small.save(PUBLIC / "favicon-96x96.png", optimize=True)
        small.save(DOCS_FAVICON, optimize=True)
        for name, size in APP_ICONS:
            (await shot(tab, square(size), size)).convert("RGB").save(PUBLIC / f"{name}.png", optimize=True)
        if args.avatar:
            (await shot(tab, square(1000), 1000)).convert("RGB").save(args.avatar, optimize=True)

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--avatar", type=Path, help="also write the 1000×1000 social avatar here")
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    asyncio.run(render(parser.parse_args()))


if __name__ == "__main__":
    main()
