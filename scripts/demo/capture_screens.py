"""Screenshot the launch pages as Alex, at 1440×900 and twice the pixels, from real runs on the demo project.

    uv run --no-project --with playwright==1.62.0 --with python-gitlab python -I scripts/demo/capture_screens.py \
      plan merge-request code-review metrics --daiv-url https://<host>:<port> --issue 1 --mr 2
    uv run ... capture_screens.py session --daiv-url ... --session <thread id> --wait-running
    uv run ... capture_screens.py egress --daiv-url ... --env <sandbox environment id>

``plan`` scrolls to DAIV's first note on the issue, ``code-review`` to the heading of its latest review on the merge
request, ``metrics`` to Insights' code velocity.
``--wait-running`` reloads the session until a run is in progress, then lets the stream fill before the shot.
Alex signs in to DAIV through GitLab, so DAIV shows only what Alex can see.
"""

import argparse
import asyncio
import time
from pathlib import Path

import gitlab
from playwright.async_api import BrowserContext, Locator, Page, async_playwright

GITLAB_URL = "http://127.0.0.1:8929"
STATE_DIR = Path.home() / ".cache" / "daiv-demo"
PROJECT = (STATE_DIR / "project").read_text().strip()
VIEWPORT = {"width": 1440, "height": 900}
SHOTS = ("plan", "merge-request", "code-review", "session", "egress", "metrics")


async def sign_in(context: BrowserContext, daiv_url: str) -> None:
    page = await context.new_page()
    await page.goto(f"{GITLAB_URL}/users/sign_in")
    await page.fill("#user_login", "alex")
    await page.fill("#user_password", (STATE_DIR / "alex-password").read_text().strip())
    await page.click("button[type=submit]")
    await page.wait_for_load_state("networkidle")
    await page.goto(f"{daiv_url}/accounts/gitlab/login/?process=login")
    await page.wait_for_load_state("networkidle")
    for name in ("Continue", "Authorize"):
        button = page.get_by_role("button", name=name)
        if await button.count():
            await button.first.click()
            await page.wait_for_load_state("networkidle")
    await page.close()
    await context.add_cookies([{"name": "super_sidebar_collapsed", "value": "true", "url": GITLAB_URL}])


async def open_gitlab(page: Page, path: str) -> None:
    await page.goto(f"{GITLAB_URL}/{PROJECT}/-/{path}")
    await page.wait_for_load_state("networkidle")
    await page.mouse.move(VIEWPORT["width"] - 5, VIEWPORT["height"] - 5)


async def scroll_to(target: Locator, offset: int) -> None:
    """GitLab scrolls an inner pane, not the document, so scroll whichever element holds the target."""
    await target.first.wait_for()
    await target.first.evaluate(
        """(el, offset) => {
          const scrolls = (e) =>
            e.scrollHeight > e.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(e).overflowY);
          let pane = el.parentElement;
          while (pane && !scrolls(pane)) pane = pane.parentElement;
          pane = pane || document.scrollingElement;
          pane.scrollTop += el.getBoundingClientRect().top - pane.getBoundingClientRect().top - offset;
        }""",
        offset,
    )
    await asyncio.sleep(1)


def bot_notes(noteable) -> list:
    return [n for n in noteable.notes.list(sort="asc", get_all=True) if n.author["username"] == "daiv" and not n.system]


async def shoot(page: Page, args: argparse.Namespace, shot: str, project) -> None:
    if shot == "plan":
        plan = bot_notes(project.issues.get(args.issue))[0]
        await open_gitlab(page, f"issues/{args.issue}")
        await scroll_to(page.locator(f"#note_{plan.id}"), 120)
    elif shot == "merge-request":
        await open_gitlab(page, f"merge_requests/{args.mr}")
    elif shot == "code-review":
        review = [n for n in bot_notes(project.mergerequests.get(args.mr)) if "daiv:code-review" in n.body][-1]
        await open_gitlab(page, f"merge_requests/{args.mr}")
        heading = page.locator(f"#note_{review.id} h2")
        details = page.locator(f"#note_{review.id} details")
        if await details.count():
            await details.first.evaluate("(d) => { d.open = true; }")
        await scroll_to(heading if await heading.count() else page.locator(f"#note_{review.id}"), 80)
    elif shot == "session":
        await open_session(page, args)
    elif shot == "egress":
        await page.goto(f"{args.daiv_url}/dashboard/sandbox-envs/{args.env}/edit/")
        await page.wait_for_load_state("networkidle")
        await scroll_to(page.locator("fieldset[x-data^=egressEditor]"), 140)
    elif shot == "metrics":
        await page.goto(f"{args.daiv_url}/dashboard/insights/?period={args.period}")
        await page.wait_for_load_state("networkidle")
        await scroll_to(page.get_by_role("heading", name="Code velocity"), 40)
    out = args.out / f"{shot}.png"
    await page.screenshot(path=str(out))
    print(out)


async def open_session(page: Page, args: argparse.Namespace) -> None:
    url = f"{args.daiv_url}/dashboard/sessions/{args.session}/"
    deadline = time.monotonic() + args.timeout
    while True:
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        status = await page.get_by_test_id("session-topbar").inner_text()
        if not args.wait_running or "Running" in status:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"no run started in session {args.session} within {args.timeout}s")
        await asyncio.sleep(3)
    await asyncio.sleep(args.settle if args.wait_running else 2)


async def capture(args: argparse.Namespace) -> None:
    gl = gitlab.Gitlab(GITLAB_URL, private_token=(STATE_DIR / "admin-token").read_text().strip())
    project = gl.projects.get(PROJECT)
    args.out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, args=["--headless=new"])
        context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2, ignore_https_errors=True)
        await sign_in(context, args.daiv_url)
        page = await context.new_page()
        for shot in args.shots:
            await shoot(page, args, shot, project)
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("shots", nargs="+", choices=SHOTS)
    parser.add_argument("--daiv-url", required=True, help="the DAIV app as the browser reaches it")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--mr", type=int)
    parser.add_argument("--session", help="the session's thread id")
    parser.add_argument("--wait-running", action="store_true")
    parser.add_argument("--settle", type=float, default=25, help="seconds of streaming before a live shot")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--env", help="the sandbox environment's id")
    parser.add_argument("--period", default="7d")
    parser.add_argument("--out", type=Path, default=STATE_DIR / "screens")
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    asyncio.run(capture(parser.parse_args()))


if __name__ == "__main__":
    main()
