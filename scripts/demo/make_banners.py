"""Render the profile banners for X, Bluesky, Mastodon, LinkedIn and YouTube, in the launch card's style.

    uv run --no-project --with playwright==1.62.0 python -I scripts/demo/make_banners.py \
      ~/.cache/daiv-demo/launch/banners

Each platform covers part of its banner with the avatar or crops it on some devices, so the text stays inside a
per-platform box and the script fails if it spills out.
"""

import argparse
import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "daiv" / "static" / "fonts" / "geist" / "geist-latin-wght-normal.woff2"

HEADLINE = "Issues in. Merge requests out."
HEADLINE_ACCENT = "On your infrastructure."
TAGLINE = "Open-source, self-hosted SWE agents for GitLab &amp; GitHub"


@dataclass
class Banner:
    name: str
    width: int
    height: int
    scale: float
    box: tuple[int, int, int, int]  # left, top, width, height in CSS px
    headline: int
    one_line: bool = False


BANNERS = [
    # X, Bluesky and Mastodon put the avatar over the lower-left corner.
    Banner("x", 1500, 500, 1, (400, 40, 1060, 400), 72),
    Banner("bluesky", 1500, 500, 2, (400, 40, 1060, 400), 72),
    Banner("mastodon", 1500, 500, 1, (400, 40, 1060, 400), 72),
    Banner("linkedin", 1128, 191, 2, (220, 20, 880, 150), 30, one_line=True),
    # YouTube shows only the centre 1546×423 on phones.
    Banner("youtube", 2560, 1440, 1, (507, 508, 1546, 423), 96),
]

OVERFLOWS = """(() => {
  const box = document.querySelector('.box').getBoundingClientRect();
  return [...document.querySelectorAll('.box > *')].some(el => {
    const r = el.getBoundingClientRect();
    return r.left < box.left || r.right > box.right || r.top < box.top || r.bottom > box.bottom;
  });
})()"""


def page(b: Banner) -> str:
    font = f"data:font/woff2;base64,{base64.b64encode(FONT.read_bytes()).decode()}"
    left, top, bw, bh = b.box
    cx = (left + bw / 2) / b.width * 100
    cy = (top + bh / 2) / b.height * 100
    h = b.headline
    sep = " " if b.one_line else "<br>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @font-face {{ font-family: Geist; src: url({font}) format("woff2"); font-weight: 100 900; }}
  * {{ margin: 0; box-sizing: border-box; }}
  html, body {{ width: {b.width}px; height: {b.height}px; background: #0D1117; overflow: hidden; }}
  body {{ position: relative; font-family: Geist, sans-serif; color: #E6EDF3; -webkit-font-smoothing: antialiased; }}
  .layer {{ position: absolute; inset: 0; }}
  .spot {{ background:
      radial-gradient(ellipse {bw * 0.55}px {bh * 0.75}px at {cx}% {cy - 4}%, rgba(139, 92, 246, 0.17) 0%,
        transparent 70%),
      radial-gradient(ellipse {bw * 0.4}px {bh * 0.6}px at {cx + 10}% {cy + 18}%, rgba(45, 212, 191, 0.07) 0%,
        transparent 70%); }}
  .dots {{ opacity: 0.35; background-image: radial-gradient(circle at 1px 1px, rgba(255,255,255,0.09) 1px,
    transparent 0); background-size: 32px 32px;
    mask-image: radial-gradient(ellipse 70% 80% at {cx}% {cy}%, #000 30%, transparent 100%); }}
  .line {{ position: absolute; height: 1px; width: {max(520, b.width * 0.35)}px; transform: rotate(28deg);
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent); }}
  .box {{ position: absolute; left: {left}px; top: {top}px; width: {bw}px; height: {bh}px; display: flex;
    flex-direction: column; align-items: center; justify-content: center; text-align: center; }}
  h1 {{ font-size: {h}px; line-height: 1.08; font-weight: 700; letter-spacing: -0.03em; color: #FFFFFF;
    white-space: nowrap; }}
  h1 span {{ background: linear-gradient(90deg, #A78BFA 0%, #8B5CF6 35%, #2DD4BF 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .tagline {{ margin-top: {h * 0.42}px; font-size: {h * 0.46}px; color: #9AA4B0; letter-spacing: -0.01em;
    white-space: nowrap; }}
</style></head><body>
  <div class="layer spot"></div>
  <div class="layer dots"></div>
  <div class="layer">
    <div class="line" style="right: -140px; top: 14%"></div>
    <div class="line" style="right: -90px; top: 24%; opacity: 0.6"></div>
    <div class="line" style="left: -140px; bottom: 30%; opacity: 0.6"></div>
  </div>
  <main class="box">
    <h1>{HEADLINE}{sep}<span>{HEADLINE_ACCENT}</span></h1>
    <p class="tagline">{TAGLINE}</p>
  </main>
</body></html>"""


async def render(args: argparse.Namespace) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, args=["--headless=new"])
        for b in BANNERS:
            context = await browser.new_context(
                viewport={"width": b.width, "height": b.height}, device_scale_factor=b.scale
            )
            tab = await context.new_page()
            await tab.set_content(page(b))
            await tab.evaluate("document.fonts.ready")
            if await tab.evaluate(OVERFLOWS):
                raise SystemExit(f"{b.name}: the text spills out of its {b.box[2]}×{b.box[3]} box")
            path = args.out / f"banner-{b.name}.png"
            await tab.screenshot(path=str(path))
            await context.close()
            print(path)
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path, help="directory for banner-<platform>.png")
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    asyncio.run(render(parser.parse_args()))


if __name__ == "__main__":
    main()
