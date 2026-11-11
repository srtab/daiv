# Demo recordings

Scripts that record the README's hero GIF and the launch video from a real DAIV run against the local GitLab, and render the launch screenshots and social preview. Every plan, commit, reply and merge request in them is DAIV's own output; only the waits are cut.

| File | What it does |
| --- | --- |
| `demo-repo/` | The small FastAPI project DAIV works on in the recording |
| `seed_gitlab.py` | Creates the demo user `alex`, the `acme/inventory-api-<n>` project, its labels and a webhook to your DAIV |
| `record_demo.py` | Drives GitLab in Chrome as Alex and records the story at 1920×1080, with a cursor, step captions and "N minutes later" badges: issue, plan, `@daiv proceed`, merge request, a review comment on a diff line, DAIV's follow-up push, merge |
| `overlay.js` | The cursor, captions and badges, injected into every page |
| `make_gif.py` | Cuts the waits out of the video and exports a GIF and an MP4; `--until hero` stops at the merge request |
| `make_card.py` | Renders the GitHub social preview, and the closing card of the video |
| `capture_screens.py` | Screenshots the issue plan, the merge request, a `/code-review` report, a live session, the sandbox egress form and Insights, signed in as Alex |

## Record

Needs the local GitLab (`docker compose --profile gitlab up`), a DAIV stack whose app the GitLab container can reach, at least one LLM key, and `ffmpeg`.

1. Create an admin token for the local GitLab once, in `~/.cache/daiv-demo/admin-token` (`chmod 600`).
2. Reset the demo project and point its webhook at your DAIV app, as the GitLab container reaches it. In a worktree slot that is the slot's app container:

    ```bash
    uv run --no-project --with python-gitlab python -I scripts/demo/seed_gitlab.py --reset \
      --hook-url https://daiv-<slot>-app-1:8000/api/codebase/callbacks/gitlab/ --app-container daiv-<slot>-app-1
    ```

3. Record. It takes as long as DAIV does, usually 10–15 minutes:

    ```bash
    uv run --no-project --with playwright==1.62.0 --with python-gitlab python -I scripts/demo/record_demo.py
    ```

    The script launches the system Chrome at a device scale of 1.5 and saves the page's own screencast frames, so the video is 1920×1080 with sharp text.

4. Export the README hero and the launch video:

    ```bash
    uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/demo.json ~/.cache/daiv-demo --until hero
    uv run --no-project --with playwright==1.62.0 python -I scripts/demo/make_card.py ~/.cache/daiv-demo/outro.png --height 720 --scale 1.5 --footer
    uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/demo.json ~/.cache/daiv-demo \
      --name demo --formats mp4 --outro ~/.cache/daiv-demo/outro.png
    ```

Run `seed_gitlab.py --reset` before every recording. It recreates the project under a new path, because DAIV keys its conversations on project path and issue number: at the old path, DAIV would resume the previous recording's conversation instead of planning again.

## Social preview and screenshots

```bash
uv run --no-project --with playwright==1.62.0 python -I scripts/demo/make_card.py ~/.cache/daiv-demo/social-preview.png
```

`capture_screens.py` shoots pages that need data from real runs on the demo project: a planned issue, an open merge request with a `@daiv /code-review` report, a session while a run is in progress (`--wait-running`), a sandbox environment with an egress policy, and merged merge requests for Insights. Its docstring has the commands.
