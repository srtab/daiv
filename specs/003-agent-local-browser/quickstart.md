# Quickstart: validating the agent local browser

This guide shows the feature working end to end. It only refers to shapes, which live elsewhere:
- shapes and texts are in the [contracts](contracts/);
- entities are in the [data model](data-model.md);
- decisions are in [research](research.md).

## Prerequisites

- **DAIV**: this branch, with `uv sync` done.
- **daiv-sandbox**: a checkout at `/home/sfr/work/personal/daiv-sandbox`, on the branch carrying the [daiv-sandbox contract](contracts/sandbox-browser-api.md). It needs the browser image built locally:

  ```bash
  docker build -t ghcr.io/srtab/daiv-sandbox-browser:dev -f browser/Dockerfile .
  ```

  The image path `browser/Dockerfile` is set by that branch.
- **Docker, with gVisor's `runsc` installed**, for the runsc passes.
- **Manual and eval scenarios**:
  - the local stack (`make up`), pointed at that daiv-sandbox build: `DAIV_SANDBOX_URL`, and the sandbox's `BROWSER_IMAGE` set to the dev tag;
  - the egress CA set up as in `docs/reference/env-variables.md`;
  - `docker/local/app/config.secrets.env` with an LLM key, as for `make integration-tests`.

## 0. Spikes (before slice 1)

Run S1–S5 from [research](research.md#spikes-to-run-first) in daiv-sandbox under runc and runsc, and record the results in `research.md`. If S2 fails, stop: the spec needs refinement before slice 1.

## 1. Gates

```bash
make lint-fix && make lint     # includes import-linter: no new contract or ignore_imports
make lint-typing
make test
```

Every test listed below must fail on `main` and pass here (`patterns/tests.md` rule 1). In daiv-sandbox, its own `make test` and the integration tests named in §3 must pass too.

## 2. DAIV unit scenarios

Run them focused while working:

```bash
uv run pytest tests/unit_tests/automation/agent/workspace/test_browser.py \
  tests/unit_tests/automation/agent/middlewares/test_browser.py \
  tests/unit_tests/automation/agent/middlewares/test_tool_media.py \
  tests/unit_tests/automation/agent/test_graph.py tests/unit_tests/core/sandbox/ \
  tests/unit_tests/sandbox_envs/ tests/unit_tests/configuration/ \
  tests/unit_tests/sessions/executor/test_run.py tests/unit_tests/sessions/test_transcript_labels.py -q
```

| Scenario | Spec | Expected |
|---|---|---|
| Site switch off, environment Browser on | US2-3, FR-017 | `workspace.browser is None`; the graph has no browser tools |
| Switch on, environment Browser off; or no sandbox; or a skip-sandbox slash command | US2-2, FR-001 | `workspace.browser is None` |
| Both on, capability 404 | US2-4, FR-001a | `workspace.browser is None`; one ERROR log per cache refresh; the config page shows the warning |
| Both on, capability true | FR-001 | `BrowserMiddleware` in the stack, before `InlineToolDefinitionsMiddleware`; its tools deferred; not in subagent stacks |
| Building the agent | FR-004 | no client call to `start_browser` until a tool is called |
| First tool call, profile `restored` with tabs | US1-5, FR-007a | `start_browser` called once with the extension archive; the result starts with the re-opened note |
| First tool call, profile `reset` | FR-007b | the result starts with the reset note |
| `ConnectionClosed` mid-call, then a second call | US4-1, FR-020 | the first call returns the stopped text; the second restarts and carries the restart note |
| Two start failures | US4-4 | the second returns "unavailable", and so does every later call; no third `start_browser` |
| Run ends: success, failure, cancel, `RunStoppedError` | US4-3, FR-009 | `stop_browser` before `close_session`; the artifact directory removed; `arelease` swallows and logs a client error |
| Specs differing only in `browser` | FR-017a | equal fingerprints; no `ENV_CHANGED` |
| `merge_sandbox_spec` with and without a per-run environment | R10 | `browser` from the per-run environment, else the global default |
| A screenshot over 5 MiB | R13 | one JPEG retake, then the too-large error |
| A model profile with `image_tool_message: False` | FR-021 | the request's image blocks are replaced with the note; the stored history is unchanged; the media is still stored |
| First model request, browser off and on | SC-006 | the system prompts differ only by the three index lines, under 2% of the input |
| Transcript labels | FR-019 | server and JS keys match; the `browser` icon is in `TRANSCRIPT_ICONS` with its SVG |
| Tool error texts | FR-020, errors rule 5 | none contains the service URL, the key or an exception repr |
| `BrowserTunnel` | R4 | bytes round-trip over a fake WebSocket server; `aclose` closes every bridge |
| Wire schemas | contract | `test_schema_consistency.py` passes with the new `_SHARED_TYPES` |

## 3. daiv-sandbox integration scenarios

Run them under runc and under runsc, with the browser image built:

| Scenario | Spec | Expected |
|---|---|---|
| A network-none session: start the browser, serve `python -m http.server 8000 --bind 127.0.0.1` in the sandbox, navigate over CDP | US1-1, FR-005 | the page loads |
| The same session, navigate to `https://example.com` | US2-5, FR-006 | the refusal page names `example.com` |
| A networked session allowing one host, with a page that loads a script from a second host | US2-6, SC-003 | the first host loads; the second request is blocked; the proxy log shows the block |
| A page `fetch` to `http://127.0.0.1:39222/json/version` and a WebSocket to the DevTools port | R4 | refused |
| Start, sign in to a local test app, stop, start | SC-008 | the cookie is present; the tab is re-opened; `profile: restored` |
| Two sessions' browsers | US2-7, FR-007 | no cookie from one is visible in the other |
| Force close, reaper removal, orphan sweep | FR-007b, SC-004 | the browser container is removed in each |
| Sandbox stop with the browser running | FR-009 | the browser container is stopped within the stop call |
| A page allocating past the browser's limit | FR-017b | only the browser container is OOM-killed; a sandbox `sleep` keeps running; stop reports `oom_killed: true` |
| A download link | FR-010 | blocked by policy |
| An old client calling `/-/capabilities/` | FR-001a | `{"browser": true}`, with no key |

## 4. Manual end-to-end

1. Turn on **Settings** → **Site configuration** → **Sandbox** → **Browser**. Then open **Settings** → **Sandbox environments**, edit the environment the local `srtab/daiv` repository uses, and set **Browser** to **On**.
2. Start a chat on a small web repository and ask: "Start the dev server and show me a screenshot of the home page."
3. Expected:
   - the transcript shows "Opened `http://localhost:<port>`" and "Took a screenshot";
   - the screenshot renders in the transcript;
   - the first browser call took under 5 s (SC-005).
4. Send "Click the sign-in link and screenshot it". The next run's first browser result lists the re-opened page (FR-007a).
5. Turn the site switch off and send another message. The run has no browser tools, and the session's sandbox is warm, not replaced (FR-017a).
6. Point `DAIV_SANDBOX_URL` at a daiv-sandbox 0.5.0, with the switch on. Runs get no browser tools, the log has one ERROR, and the Sandbox group shows the warning (FR-001a).

## 5. Evals (Constitution V)

```bash
make eval-prompts SUITES="code_review" OUT=eval-runs/browser-before.jsonl   # browser off, on main, before the change
make eval-prompts SUITES="code_review" OUT=eval-runs/browser-after.jsonl    # this branch, browser on
uv run evals/compare_runs.py eval-runs/browser-before.jsonl eval-runs/browser-after.jsonl
make eval-prompts SUITES="browser" OUT=eval-runs/browser-cases.jsonl        # SC-001, SC-002
```

The suite list is confirmed in the first eval task (research R17). `browser` is a new marker in `pyproject.toml`. An `unconfirmed` verdict is confirmed with the re-run that `compare_runs.py` prints, before anything is claimed.
