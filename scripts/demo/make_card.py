"""Render the launch card: the GitHub social preview (1280×640) and the video's closing frame (1920×1080).

    uv run --no-project --with playwright==1.62.0 python -I scripts/demo/make_card.py \
      ~/.cache/daiv-demo/social-preview.png
    uv run --no-project --with playwright==1.62.0 python -I scripts/demo/make_card.py \
      ~/.cache/daiv-demo/outro.png --height 720 --scale 1.5 --footer

GitHub crops the preview to its centre on some surfaces, so everything sits inside a 1000×460 box in the middle.
"""

import argparse
import asyncio
import base64
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
LOGO = ROOT / "docs" / "assets" / "logo.svg"
FONTS = ROOT / "daiv" / "static" / "fonts" / "geist"
ICONS = ROOT / "daiv" / "core" / "static" / "core" / "img" / "icons"

HEADLINE = "Issues in. Merge requests out."
HEADLINE_ACCENT = "On your infrastructure."
TAGLINE = "Open-source, self-hosted SWE agents for GitLab &amp; GitHub"
FOOTER = "daivagent.com  ·  github.com/srtab/daiv"


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def icon(name: str) -> str:
    return f'<span class="icon" style="--icon: url({data_uri(ICONS / f"{name}.svg", "image/svg+xml")})"></span>'


def page(width: int, height: int, footer: bool) -> str:
    sans = data_uri(FONTS / "geist-latin-wght-normal.woff2", "font/woff2")
    mono = data_uri(FONTS / "geist-mono-latin-wght-normal.woff2", "font/woff2")
    footer_html = f'<p class="footer">{FOOTER}</p>' if footer else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @font-face {{ font-family: Geist; src: url({sans}) format("woff2"); font-weight: 100 900; }}
  @font-face {{ font-family: "Geist Mono"; src: url({mono}) format("woff2"); font-weight: 100 900; }}
  * {{ margin: 0; box-sizing: border-box; }}
  html, body {{ width: {width}px; height: {height}px; background: #0D1117; overflow: hidden; }}
  body {{ position: relative; display: grid; place-items: center; font-family: Geist, sans-serif; color: #E6EDF3;
    -webkit-font-smoothing: antialiased; }}
  .layer {{ position: absolute; inset: 0; pointer-events: none; }}
  .spot {{ background:
      radial-gradient(ellipse 55% 60% at 50% 42%, rgba(139, 92, 246, 0.16) 0%, transparent 70%),
      radial-gradient(ellipse 40% 45% at 62% 70%, rgba(45, 212, 191, 0.07) 0%, transparent 70%); }}
  .dots {{ opacity: 0.35; background-image: radial-gradient(circle at 1px 1px, rgba(255,255,255,0.09) 1px,
    transparent 0); background-size: 32px 32px;
    mask-image: radial-gradient(ellipse 70% 70% at 50% 50%, #000 30%, transparent 100%); }}
  .line {{ position: absolute; height: 1px; width: 520px; transform: rotate(28deg);
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent); }}
  .card {{ position: relative; width: 1000px; height: 460px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; }}
  .logo {{ height: 54px; }}
  h1 {{ margin-top: 40px; font-size: 58px; line-height: 1.08; font-weight: 700; letter-spacing: -0.03em;
    color: #FFFFFF; }}
  h1 span {{ background: linear-gradient(90deg, #A78BFA 0%, #8B5CF6 35%, #2DD4BF 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .tagline {{ margin-top: 26px; font-size: 25px; font-weight: 400; color: #9AA4B0; letter-spacing: -0.01em; }}
  .platforms {{ margin-top: 30px; display: flex; gap: 22px; align-items: center; color: #9AA4B0;
    font: 600 13px/1 "Geist Mono", monospace; letter-spacing: 0.14em; text-transform: uppercase; }}
  .platforms div {{ display: flex; gap: 10px; align-items: center; }}
  .platforms .plus {{ color: #4B5563; font: 300 18px/1 Geist, sans-serif; }}
  .icon {{ width: 22px; height: 22px; background: #9AA4B0; mask: var(--icon) no-repeat center / contain;
    -webkit-mask: var(--icon) no-repeat center / contain; }}
  .footer {{ position: absolute; bottom: 44px; left: 0; right: 0; text-align: center; white-space: pre;
    font: 500 16px/1 "Geist Mono", monospace; color: #878F9C; letter-spacing: 0.02em; }}
</style></head><body>
  <div class="layer spot"></div>
  <div class="layer dots"></div>
  <div class="layer">
    <div class="line" style="right: -140px; top: 18%"></div>
    <div class="line" style="right: -90px; top: 27%; opacity: 0.6"></div>
    <div class="line" style="left: -140px; bottom: 22%"></div>
    <div class="line" style="left: -90px; bottom: 14%; opacity: 0.6"></div>
  </div>
  <main class="card">
    <img class="logo" src="{data_uri(LOGO, "image/svg+xml")}" alt="DAIV">
    <h1>{HEADLINE}<br><span>{HEADLINE_ACCENT}</span></h1>
    <p class="tagline">{TAGLINE}</p>
    <div class="platforms">
      <div>{icon("gitlab")}GitLab</div><span class="plus">+</span><div>{icon("github")}GitHub</div>
    </div>
  </main>
  {footer_html}
</body></html>"""


async def render(args: argparse.Namespace) -> None:
    html = page(args.width, args.height, args.footer)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, args=["--headless=new"])
        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height}, device_scale_factor=args.scale
        )
        tab = await context.new_page()
        await tab.set_content(html)
        await tab.evaluate("document.fonts.ready")
        await tab.screenshot(path=str(args.out))
        await browser.close()
    print(args.out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--scale", type=float, default=1, help="device pixels per CSS pixel")
    parser.add_argument("--footer", action="store_true", help="add the website and repository addresses")
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    asyncio.run(render(parser.parse_args()))


if __name__ == "__main__":
    main()
