# Demo recordings

Scripts that record the README's hero GIF from a real DAIV run against the local GitLab. Every plan, commit and merge request in the recording is DAIV's own output; only the waits are cut.

| File | What it does |
| --- | --- |
| `demo-repo/` | The small FastAPI project DAIV works on in the recording |
| `seed_gitlab.py` | Creates the demo user `alex`, the `acme/inventory-api-<n>` project, its labels and a webhook to your DAIV |
| `record_hero.py` | Drives GitLab in Chrome as Alex and records the video, with a cursor, step captions and "N minutes later" badges |
| `overlay.js` | The cursor, captions and badges, injected into every page |
| `make_gif.py` | Cuts the waits out of the video and exports `hero.gif` and `hero.mp4` |

## Record

Needs the local GitLab (`docker compose --profile gitlab up`), a DAIV stack whose app the GitLab container can reach, at least one LLM key, and `ffmpeg`.

1. Create an admin token for the local GitLab once, in `~/.cache/daiv-demo/admin-token` (`chmod 600`).
2. Reset the demo project and point its webhook at your DAIV app, as the GitLab container reaches it. In a worktree slot that is the slot's app container:

    ```bash
    uv run --no-project --with python-gitlab python -I scripts/demo/seed_gitlab.py --reset \
      --hook-url https://daiv-<slot>-app-1:8000/api/codebase/callbacks/gitlab/ --app-container daiv-<slot>-app-1
    ```

3. Record. It takes as long as DAIV does, usually 5–8 minutes:

    ```bash
    uv run --no-project --with playwright==1.62.0 --with python-gitlab python -I scripts/demo/record_hero.py
    ```

    Playwright 1.62 is the last release that uses the ffmpeg build already in `~/.cache/ms-playwright`. The script launches the system Chrome, so the bundled Chromium's version doesn't matter.

4. Export:

    ```bash
    uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/hero.json ~/.cache/daiv-demo
    ```

Run `seed_gitlab.py --reset` before every recording. It recreates the project under a new path, because DAIV keys its conversations on project path and issue number: at the old path, DAIV would resume the previous recording's conversation instead of planning again.
