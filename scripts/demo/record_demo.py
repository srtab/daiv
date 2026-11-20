"""Record the demo: label an issue, DAIV plans, `@daiv proceed`, DAIV opens the merge request, a reviewer asks for a
change on a diff line, DAIV pushes it, and the reviewer merges.

Needs the demo project from ``seed_gitlab.py`` (run it with ``--reset`` first) and a DAIV stack receiving its
webhooks. Every agent step is real; the waits are cut afterwards by ``make_gif.py`` using the ``skip`` segments
written next to the video. The ``hero`` mark ends the README hero at the merge request.

    uv run --no-project --with playwright==1.62.0 --with python-gitlab python -I scripts/demo/record_demo.py
"""

import argparse
import asyncio
import base64
import contextlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import gitlab
from playwright.async_api import CDPSession, Error, Locator, Page, async_playwright

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
REVIEW_SETTING = "RATE_LIMIT_PER_MINUTE"
REVIEW_COMMENT = (
    f"@daiv make the limit configurable with a `{REVIEW_SETTING}` environment variable, keeping 60 as the default."
)

PICK_REVIEW_LINE_JS = """() => {
  const rows = [...document.querySelectorAll("tr[data-hunk-lines]")]
    .filter((row) => row.querySelector('td.rd-line-content[data-change="added"]'));
  const text = (row) => row.querySelector("td.rd-line-content").textContent.trim();
  const code = rows.filter((row) => /\\b60\\b/.test(text(row)) && !text(row).startsWith("#"));
  const row = code.find((r) => text(r).includes("=")) || code[0] || rows.find((r) => /\\b60\\b/.test(text(r)));
  return row ? row.id : null;
}"""


class Timeline:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.wall_start = time.time()
        self.skips: list[dict] = []
        self.marks: dict[str, float] = {}

    def now(self) -> float:
        return round(time.monotonic() - self.start, 3)

    def skip_from(self) -> float:
        return self.now()

    def skip_to(self, start: float, label: str) -> str:
        end = self.now()
        self.skips.append({"start": start, "end": end, "label": label})
        minutes = max(round((end - start) / 60), 1)
        return f"{minutes} minute{'s' if minutes > 1 else ''} later"

    def mark(self, name: str) -> None:
        self.marks[name] = self.now()


class Screencast:
    """The page's own frames, at device pixels: Playwright's video captures CSS pixels only, at a low bitrate."""

    def __init__(self, cdp: CDPSession, frames: Path, timeline: Timeline) -> None:
        self.cdp = cdp
        self.frames = frames
        self.timeline = timeline
        self.times: list[float] = []
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir(parents=True)
        cdp.on("Page.screencastFrame", self.on_frame)

    def on_frame(self, params: dict) -> None:
        (self.frames / f"{len(self.times):06d}.jpg").write_bytes(base64.b64decode(params["data"]))
        self.times.append(params["metadata"]["timestamp"] - self.timeline.wall_start)
        asyncio.ensure_future(self.ack(params["sessionId"]))

    async def ack(self, session_id: int) -> None:
        with contextlib.suppress(Error):
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})

    async def start(self, scale: float) -> None:
        size = {"maxWidth": round(VIEWPORT["width"] * scale), "maxHeight": round(VIEWPORT["height"] * scale)}
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, **size})

    async def stop(self) -> None:
        await self.cdp.send("Page.stopScreencast")

    def encode(self, video: Path, end: float) -> None:
        """Hold each frame until the next one, so the video's clock is the timeline's and the skips line up."""
        listing = self.frames / "frames.txt"
        lines = []
        for i, start in enumerate(self.times):
            until = self.times[i + 1] if i + 1 < len(self.times) else end
            lines += [f"file '{i:06d}.jpg'", f"duration {max(until - max(start, 0), 0):.4f}"]
        lines[1] = f"duration {(self.times[1] if len(self.times) > 1 else end):.4f}"
        lines.append(f"file '{len(self.times) - 1:06d}.jpg'")
        listing.write_text("\n".join(lines) + "\n")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vf",
                "fps=30,format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "12",
                "-preset",
                "veryfast",
                str(video),
            ],
            check=True,
        )


class Demo:
    def __init__(self, page: Page, timeline: Timeline, gl: gitlab.Gitlab) -> None:
        self.page = page
        self.timeline = timeline
        self.project = gl.projects.get(PROJECT)

    async def caption(self, step: str, text: str) -> None:
        await self.page.evaluate("([s, t]) => window.__demo.caption(s, t)", [step, text])

    async def badge(self, text: str) -> None:
        await self.page.evaluate("(t) => window.__demo.badge(t)", text)

    async def hover(self, target: Locator) -> None:
        box = await target.bounding_box()
        await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)

    async def click(self, target: Locator, pause: float = 0.35) -> None:
        await target.scroll_into_view_if_needed()
        await self.hover(target)
        await asyncio.sleep(pause)
        await self.page.mouse.down()
        await self.page.mouse.up()

    async def scroll_to(self, target: Locator, offset: int = 90, duration_ms: int = 900) -> None:
        top = await target.evaluate("(el) => window.__demo.topOf(el)")
        await self.page.evaluate("([y, d]) => window.__demo.smoothScroll(y, d)", [max(top - offset, 0), duration_ms])
        await asyncio.sleep(duration_ms / 1000 + 0.1)

    async def open(self, url: str) -> None:
        await self.page.goto(url)
        await self.page.wait_for_load_state("networkidle")

    async def wait_for_bot_note(self, noteable, after: int, timeout: int = 1200) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for note in noteable().notes.list(sort="asc", get_all=True):
                if note.author["username"] == "daiv" and not note.system and note.id > after:
                    return note.attributes
            await asyncio.sleep(5)
        raise TimeoutError(f"DAIV did not answer within {timeout}s")

    async def wait_for_pipeline(self, mr_iid: int, sha: str | None = None, timeout: int = 600) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pipeline = self.project.mergerequests.get(mr_iid).head_pipeline
            if (
                pipeline
                and (sha is None or pipeline["sha"] == sha)
                and pipeline["status"] in ("success", "failed", "canceled", "skipped")
            ):
                return pipeline["status"]
            await asyncio.sleep(5)
        return None

    async def wait_for_merge(self, mr_iid: int, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while self.project.mergerequests.get(mr_iid).state != "merged":
            if time.monotonic() > deadline:
                raise TimeoutError(f"merge request !{mr_iid} did not merge within {timeout}s")
            await asyncio.sleep(1)

    def issue(self, iid: int):
        return lambda: self.project.issues.get(iid)

    def merge_request(self, iid: int):
        return lambda: self.project.mergerequests.get(iid)

    @staticmethod
    def last_note_id(noteable) -> int:
        return max((n.id for n in noteable().notes.list(get_all=True)), default=0)


async def sign_in(browser, password: str) -> dict:
    login = await browser.new_context(viewport=VIEWPORT)
    page = await login.new_page()
    await page.goto(f"{GITLAB_URL}/users/sign_in")
    await page.fill("#user_login", "alex")
    await page.fill("#user_password", password)
    await page.click("button[type=submit]")
    await page.wait_for_load_state("networkidle")
    storage = await login.storage_state()
    await login.close()
    return storage


async def set_viewport(cdp: CDPSession) -> None:
    """Playwright's own viewport would emulate a scale of 1; scale 0 keeps the browser's forced device scale."""
    await cdp.send("Emulation.setDeviceMetricsOverride", {**VIEWPORT, "deviceScaleFactor": 0, "mobile": False})


async def request_change(demo: Demo, mr_iid: int) -> int:
    page = demo.page
    row_id = await page.evaluate(PICK_REVIEW_LINE_JS)
    row = page.locator(f"tr#{row_id}") if row_id else page.locator("tr[data-hunk-lines]").first
    await demo.scroll_to(row, offset=260, duration_ms=1000)
    await demo.caption("5", "Ask for a change on any line")
    await asyncio.sleep(1.0)
    await demo.hover(row.locator("td.rd-line-content"))
    await asyncio.sleep(0.5)
    await demo.click(row.get_by_test_id("new_discussion_toggle"))
    await asyncio.sleep(0.6)
    await demo.scroll_to(row, offset=110, duration_ms=700)
    editor = page.locator('[contenteditable="true"][aria-label="Reply to comment"]').last
    await demo.click(editor)
    await page.keyboard.type(REVIEW_COMMENT, delay=45)
    await asyncio.sleep(0.6)
    await demo.click(page.get_by_test_id("reply-comment-button").last)
    await asyncio.sleep(2.0)
    return Demo.last_note_id(demo.merge_request(mr_iid))


async def record(args) -> None:
    gl = gitlab.Gitlab(GITLAB_URL, private_token=(STATE_DIR / "admin-token").read_text().strip())
    password = (STATE_DIR / "alex-password").read_text().strip()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    issue = gl.projects.get(PROJECT).issues.create({"title": ISSUE_TITLE, "description": ISSUE_BODY}, sudo="alex")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=args.chrome,
            args=["--headless=new", f"--force-device-scale-factor={args.scale}", "--window-size=1280,800"],
        )
        storage = await sign_in(browser, password)

        ctx = await browser.new_context(no_viewport=True, storage_state=storage)
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await set_viewport(cdp)
        timeline = Timeline()
        screencast = Screencast(cdp, out / "frames", timeline)
        await screencast.start(args.scale)
        demo = Demo(page, timeline, gl)

        warmup = timeline.skip_from()
        await demo.open(f"{GITLAB_URL}/{PROJECT}/-/issues/{issue.iid}")
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
        plan = await demo.wait_for_bot_note(demo.issue(issue.iid), 0)
        await page.reload()
        await page.wait_for_load_state("networkidle")
        note = page.locator(f"#note_{plan['id']}")
        await note.first.wait_for()
        await demo.scroll_to(note.first, duration_ms=10)
        await demo.badge(timeline.skip_to(wait, "plan"))
        await demo.caption("2", "DAIV reads the code and posts a plan")
        await asyncio.sleep(2.5)
        await page.evaluate("() => window.__demo.smoothScrollBy(420, 3000)")
        await asyncio.sleep(3.4)

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
        proceed_note = demo.last_note_id(demo.issue(issue.iid))

        wait = timeline.skip_from()
        done = await demo.wait_for_bot_note(demo.issue(issue.iid), proceed_note)
        mr = demo.project.mergerequests.list(state="opened", order_by="created_at", sort="desc")[0]
        pipeline = await demo.wait_for_pipeline(mr.iid)
        mr_url = f"{GITLAB_URL}/{PROJECT}/-/merge_requests/{mr.iid}"
        await demo.open(mr_url)
        await asyncio.sleep(1.5)
        await demo.badge(timeline.skip_to(wait, "implementation"))

        tests = ", tests passing" if pipeline == "success" else ""
        await demo.caption("4", f"DAIV opens the merge request{tests}")
        await asyncio.sleep(3.2)
        await demo.click(page.get_by_role("link", name="Changes"))
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.6)
        await page.evaluate("() => window.__demo.smoothScrollBy(320, 3000)")
        await asyncio.sleep(3.6)
        timeline.mark("hero")

        head = demo.project.mergerequests.get(mr.iid).sha
        comment = await request_change(demo, mr.iid)

        wait = timeline.skip_from()
        reply = await demo.wait_for_bot_note(demo.merge_request(mr.iid), comment)
        pushed = demo.project.mergerequests.get(mr.iid).sha
        pipeline = await demo.wait_for_pipeline(mr.iid, pushed) if pushed != head else pipeline
        await demo.open(mr_url)
        note = page.locator(f"#note_{reply['id']}").first
        await note.wait_for()
        await demo.scroll_to(note, offset=380, duration_ms=10)
        await asyncio.sleep(1.0)
        await demo.badge(timeline.skip_to(wait, "review"))
        await demo.caption("6", "DAIV pushes the change and replies")
        await asyncio.sleep(3.0)
        await page.evaluate("() => window.__demo.smoothScrollBy(220, 1800)")
        await asyncio.sleep(2.4)
        await demo.click(page.get_by_role("link", name="Changes"))
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.2)
        setting = page.locator("td.rd-line-content[data-change='added']", has_text=REVIEW_SETTING).first
        if await setting.count():
            await demo.scroll_to(setting, offset=280, duration_ms=1400)
        await asyncio.sleep(3.0)

        await demo.click(page.get_by_role("link", name="Overview"))
        await page.wait_for_load_state("networkidle")
        # Until its merge check settles, GitLab offers "Set to auto-merge" on the same button.
        merge = page.get_by_test_id("merge-button").filter(has_text=re.compile(r"^\s*Merge\s*$"))
        await merge.wait_for(timeout=60_000)
        await page.wait_for_function("(b) => !b.disabled", arg=await merge.element_handle())
        await demo.scroll_to(merge, offset=360, duration_ms=800)
        tests = "Tests pass again, merge it" if pipeline == "success" else "Merge it"
        await demo.caption("7", tests)
        await asyncio.sleep(1.4)
        await demo.click(merge)
        clicked = timeline.now()
        try:
            await demo.wait_for_merge(mr.iid, timeout=10)
        except TimeoutError:
            # GitLab's widget sometimes spins forever after the click; merge as Alex and cut the spin.
            demo.project.mergerequests.get(mr.iid).merge(should_remove_source_branch=True, sudo="alex")
            await demo.wait_for_merge(mr.iid)
            await page.reload()
            await page.wait_for_load_state("networkidle")
            timeline.skip_to(clicked + 1.0, "merge")
        await asyncio.sleep(3.5)

        end = timeline.now()
        await screencast.stop()
        await ctx.close()
        await browser.close()

    video = out / "demo.mp4"
    screencast.encode(video, end)
    shutil.rmtree(screencast.frames)
    (out / "demo.json").write_text(
        json.dumps(
            {
                "video": video.name,
                "skips": timeline.skips,
                "marks": timeline.marks,
                "issue": issue.iid,
                "merge_request": mr.iid,
                "note": done["id"],
            },
            indent=2,
        )
    )
    print(f"recorded {video} with {len(timeline.skips)} cuts")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(STATE_DIR / "recordings"))
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    parser.add_argument("--scale", type=float, default=1.5, help="device pixels per CSS pixel; 1.5 records 1080p")
    asyncio.run(record(parser.parse_args()))


if __name__ == "__main__":
    main()
