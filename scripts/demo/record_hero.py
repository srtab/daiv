"""Record the README hero: label an issue, DAIV plans, `@daiv proceed`, DAIV opens the merge request.

Needs the demo project from ``seed_gitlab.py`` (run it with ``--reset`` first) and a DAIV stack
receiving its webhooks. Every agent step is real; the waits are cut afterwards by ``make_gif.py``
using the ``skip`` segments written next to the video.

    uv run --no-project --with playwright==1.62.0 --with python-gitlab python -I scripts/demo/record_hero.py
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import gitlab
from playwright.async_api import Locator, Page, async_playwright

GITLAB_URL = "http://127.0.0.1:8929"
STATE_DIR = Path.home() / ".cache" / "daiv-demo"
PROJECT = (STATE_DIR / "project").read_text().strip()
OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text()
VIEWPORT = {"width": 1280, "height": 720}

ISSUE_TITLE = "Add rate limiting to the API endpoints"
ISSUE_BODY = (
    "A client script polled `GET /items` in a tight loop yesterday and slowed the service down for everyone.\n\n"
    "Limit each client, by IP address, to 60 requests per minute across the API. Over the limit, respond with "
    "`429 Too Many Requests` and a `Retry-After` header. `/health` stays unlimited so the load balancer checks "
    "keep working."
)


class Timeline:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.skips: list[dict] = []
        self.captions: list[dict] = []

    def now(self) -> float:
        return round(time.monotonic() - self.start, 3)

    def skip_from(self) -> float:
        return self.now()

    def skip_to(self, start: float, label: str) -> str:
        end = self.now()
        self.skips.append({"start": start, "end": end, "label": label})
        minutes = max(round((end - start) / 60), 1)
        return f"{minutes} minute{'s' if minutes > 1 else ''} later"


class Demo:
    def __init__(self, page: Page, timeline: Timeline, gl: gitlab.Gitlab) -> None:
        self.page = page
        self.timeline = timeline
        self.project = gl.projects.get(PROJECT)

    async def caption(self, step: str, text: str) -> None:
        await self.page.evaluate("([s, t]) => window.__demo.caption(s, t)", [step, text])

    async def badge(self, text: str) -> None:
        await self.page.evaluate("(t) => window.__demo.badge(t)", text)

    async def click(self, target: Locator, pause: float = 0.35) -> None:
        await target.scroll_into_view_if_needed()
        box = await target.bounding_box()
        await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
        await asyncio.sleep(pause)
        await self.page.mouse.down()
        await self.page.mouse.up()

    async def scroll_to(self, target: Locator, offset: int = 90, duration_ms: int = 900) -> None:
        top = await target.evaluate("(el) => window.__demo.topOf(el)")
        await self.page.evaluate("([y, d]) => window.__demo.smoothScroll(y, d)", [max(top - offset, 0), duration_ms])
        await asyncio.sleep(duration_ms / 1000 + 0.1)

    async def wait_for_bot_note(self, issue_iid: int, after: int, timeout: int = 1200) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            issue = self.project.issues.get(issue_iid)
            for note in issue.notes.list(sort="asc", get_all=True):
                if note.author["username"] == "daiv" and not note.system and note.id > after:
                    return note.attributes
            await asyncio.sleep(5)
        raise TimeoutError(f"DAIV did not answer on issue {issue_iid} within {timeout}s")

    async def wait_for_pipeline(self, mr_iid: int, timeout: int = 600) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pipeline = self.project.mergerequests.get(mr_iid).head_pipeline
            if pipeline and pipeline["status"] in ("success", "failed", "canceled", "skipped"):
                return pipeline["status"]
            await asyncio.sleep(5)
        return None

    def last_note_id(self, issue_iid: int) -> int:
        return max((n.id for n in self.project.issues.get(issue_iid).notes.list(get_all=True)), default=0)


async def record(args) -> None:
    gl = gitlab.Gitlab(GITLAB_URL, private_token=(STATE_DIR / "admin-token").read_text().strip())
    password = (STATE_DIR / "alex-password").read_text().strip()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    issue = gl.projects.get(PROJECT).issues.create({"title": ISSUE_TITLE, "description": ISSUE_BODY}, sudo="alex")

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, args=["--headless=new"])

        login = await browser.new_context(viewport=VIEWPORT)
        page = await login.new_page()
        await page.goto(f"{GITLAB_URL}/users/sign_in")
        await page.fill("#user_login", "alex")
        await page.fill("#user_password", password)
        await page.click("button[type=submit]")
        await page.wait_for_load_state("networkidle")
        storage = await login.storage_state()
        await login.close()

        ctx = await browser.new_context(
            viewport=VIEWPORT, storage_state=storage, record_video_dir=str(out), record_video_size=VIEWPORT
        )
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        timeline = Timeline()
        demo = Demo(page, timeline, gl)

        warmup = timeline.skip_from()
        await page.goto(f"{GITLAB_URL}/{PROJECT}/-/issues/{issue.iid}")
        await page.wait_for_load_state("networkidle")
        sidebar_toggle = page.get_by_test_id("super-sidebar-collapse-button")
        if await sidebar_toggle.is_visible():
            await sidebar_toggle.click()
        await asyncio.sleep(1)
        timeline.skip_to(warmup, "page load")

        await demo.caption("1", "Label an issue with <code>daiv</code>")
        await asyncio.sleep(1.2)
        labels = page.get_by_test_id("work-item-labels")
        await demo.click(labels.get_by_role("button", name="Edit"))
        await asyncio.sleep(0.6)
        await page.keyboard.type("dai", delay=90)
        await asyncio.sleep(0.5)
        await demo.click(page.get_by_role("option", name="daiv"))
        await asyncio.sleep(0.4)
        await demo.click(page.get_by_role("heading", name=ISSUE_TITLE))
        await asyncio.sleep(1.5)

        wait = timeline.skip_from()
        plan = await demo.wait_for_bot_note(issue.iid, 0)
        await page.reload()
        await page.wait_for_load_state("networkidle")
        note = page.locator(f"#note_{plan['id']}")
        await note.first.wait_for()
        await demo.scroll_to(note.first, duration_ms=10)
        await demo.badge(timeline.skip_to(wait, "plan"))
        await demo.caption("2", "DAIV reads the code and posts a plan")
        await asyncio.sleep(2.2)
        await page.evaluate("() => window.__demo.smoothScrollBy(380, 2200)")
        await asyncio.sleep(2.6)

        await demo.caption("3", "Approve it with <code>@daiv proceed</code>")
        editor = page.get_by_test_id("content_editor_editablebox").last
        await demo.scroll_to(editor, offset=320, duration_ms=700)
        await demo.click(editor)
        await page.keyboard.type("@daiv proceed", delay=70)
        await asyncio.sleep(0.4)
        await demo.click(
            page.get_by_test_id("work-item-comment-form-actions").get_by_role("button", name="Comment", exact=True)
        )
        await asyncio.sleep(1.5)
        proceed_note = demo.last_note_id(issue.iid)

        wait = timeline.skip_from()
        done = await demo.wait_for_bot_note(issue.iid, proceed_note)
        mr = demo.project.mergerequests.list(state="opened", order_by="created_at", sort="desc")[0]
        pipeline = await demo.wait_for_pipeline(mr.iid)
        await page.goto(f"{GITLAB_URL}/{PROJECT}/-/merge_requests/{mr.iid}")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.5)
        await demo.badge(timeline.skip_to(wait, "implementation"))

        tests = ", tests passing" if pipeline == "success" else ""
        await demo.caption("4", f"DAIV opens the merge request{tests}")
        await asyncio.sleep(2.8)
        await demo.click(page.get_by_role("link", name="Changes"))
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.6)
        await page.evaluate("() => window.__demo.smoothScrollBy(320, 3000)")
        await asyncio.sleep(3.6)

        await ctx.close()
        await browser.close()

    video = next(out.glob("*.webm"))
    video = video.rename(out / "hero.webm")
    (out / "hero.json").write_text(
        json.dumps({"video": video.name, "skips": timeline.skips, "issue": issue.iid, "note": done["id"]}, indent=2)
    )
    print(f"recorded {video} with {len(timeline.skips)} cuts")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(STATE_DIR / "recordings"))
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    asyncio.run(record(parser.parse_args()))


if __name__ == "__main__":
    main()
