# Research: Agent Local Browser

Decisions R1–R17 below map the [spec](spec.md) onto two codebases: DAIV at `84567d049`, and daiv-sandbox at `d1d5ef5` (`main`). Each decision names what was chosen, why, and what was rejected. Spikes S1–S5 at the end verify the assumptions no source can settle. The plan's first slice runs them before any production code.

Sources read:
- DAIV: the sandbox session lifecycle (`automation/agent/workspace/session.py`, `executor/run.py`), the sandbox client and schemas (`core/sandbox/`), sandbox environments (`sandbox_envs/`), agent assembly (`automation/agent/graph.py`, `subagents.py`, `deferred/`), tool media, transcript labels and site settings.
- daiv-sandbox: the API (`daiv_sandbox/main.py`, `schemas.py`), sessions and the reaper (`sessions.py`, `reaper.py`), and the egress proxy (`egress/`, `egress_proxy/`).
- The wheels of `langchain-stagehand` 0.0.1, `stagehand` 4.4.0 and `stagehand-middleware` 0.1.2, read without running them.

## R1. The browser gets a container of its own, beside the sandbox

**Decision**: daiv-sandbox runs the browser in a **browser container**, one per sandbox session, created on the session's first browser call. The browser container:
- uses the sandbox's runtime (`RUNTIME`, runc or runsc) and its own memory and CPU limits;
- runs a DAIV-provided browser image, `ghcr.io/srtab/daiv-sandbox-browser`;
- carries the labels `daiv.sandbox.type=browser` and `daiv.sandbox.session=<sandbox container id>`.

The sandbox container itself does not change.

**Rationale**:
- **FR-017b.** Under gVisor, the host cgroup limits the whole sandbox. A browser inside the sandbox container that ran out of memory would take the agent's commands and dev server down with it. A separate container fails alone.
- **FR-008.** The profile sits in the browser container's filesystem, which the agent's `bash` cannot read.
- **FR-005.** The base image does not matter: the browser image carries Chromium, its libraries and the relay.

**Alternatives considered**:
- **Chromium inside the sandbox container**, shipped as a self-contained bundle with its own loader. Rejected: it shares the sandbox's memory limit, so under gVisor an OOM kills the sandbox, and the profile would be readable by `bash`.
- **A browser container that joins the sandbox's network namespace** (`network_mode: container:<id>`). Rejected: under runsc, each container has its own gVisor network stack, so loopback is not shared. A network-none sandbox also cannot be joined to a network without being replaced, which FR-017a forbids. It would work under runc only, and DAIV supports both.
- **The browser in DAIV's worker.** Rejected by the 2026-11-14 clarification.

## R2. Local services are reached through a relay the service pumps

**Decision**: The browser reaches the sandbox's loopback through a byte relay, `daiv-relay`. It is a static binary with no libc dependency, shipped in the browser image. It has three parts:
- **Inside the browser container**, `daiv-relay listen` serves an HTTP proxy on `127.0.0.1:39223`. Chrome's proxy auto-config (PAC) sends every loopback destination (`localhost`, `*.localhost`, `127.0.0.1`, `[::1]`) there, except the DevTools port (R4). The flag `--proxy-bypass-list=<-loopback>` removes Chrome's built-in loopback bypass.
- **Inside the sandbox container**, `daiv-relay serve` dials `127.0.0.1:<port>` for each relayed connection. daiv-sandbox copies the binary in with `put_archive` (`/tmp/.daiv/relay`, owned by the sandbox user) when the browser starts, so it works on any base image.
- **daiv-sandbox** starts both processes as `exec` streams, and pumps bytes between their stdio. The two processes multiplex the connections. The service is only a byte pump and parses nothing.

**Rationale**: This is the only design that reaches a service bound to the sandbox's `127.0.0.1` under both runtimes, on network-none sandboxes, without replacing the sandbox (FR-005, FR-017a). The 2026-10-29 spike showed an `exec -i` stdio relay works under runsc with no network.

**Alternatives considered**:
- **A per-session internal network** between the sandbox and the browser, with a listener in the sandbox. Rejected for three reasons:
  - It needs an interface on network-none sandboxes, which means replacing them.
  - Each session would take one more Docker bridge subnet, and DAIV already hits address-pool exhaustion (DAIV-BQ/BR).
  - Dev servers bound to `127.0.0.1` would still need a relay.
- **Mirroring the sandbox's listening ports** into the browser container by polling `/proc/net/tcp`. Rejected: it races a server that is still starting, and needs a poller.
- **Answering loopback requests through CDP request interception.** Rejected: it breaks WebSockets, so dev-server live reload stops working, and it puts DAIV in the data path.

**Risk**: Chrome's PAC handling of loopback and the relay's throughput, including live-reload WebSockets, are verified by spike S2.

## R3. External egress follows the session's egress proxy

**Decision**:
- **When the session has an egress proxy** (a networked environment, or a run that DAIV opened for its git platform):
  - the browser container joins the session's internal network (`daiv-egress-<token>`);
  - the PAC sends every non-loopback destination to the proxy, at the address the service resolves when the browser starts;
  - the operator CA goes into the browser's NSS database with `certutil`, because Chromium does not read `/etc/ssl/certs`.
- **When the session has no proxy**, the browser container runs with `network_mode=none`. The PAC sends non-loopback destinations to `daiv-relay`'s refusal page, which says `<host> is not allowed by this environment's network policy`.
- daiv-sandbox's egress addon changes its 403 text from `egress: destination not allowed` to one that names the host (FR-006).

**Rationale**: The proxy already enforces the environment's policy, fails closed and hot-reloads. Every page request goes through Chrome's network stack, including requests made by run code and by the Stagehand extension's service worker, so the PAC governs all of them. A policy change through `PUT /session/{id}/egress/` applies to the browser with nothing new.

**Consequence**: The browser has the same effective egress as the sandbox's commands. That includes the git-platform rule DAIV adds at run time, which injects the run's git credential. A page on an allowed host can therefore send credentialed requests to the git platform. This adds no capability: package scripts the agent runs (`npm install`, `pip install`) already execute untrusted code with the same egress and the same injection. The documentation says so, and spec refinement 3 records it.

**Alternatives considered**: A per-client policy in the proxy addon, so the browser gets the environment's policy without DAIV's git rule. Deferred, not rejected: it is a larger change to the egress addon. It is the follow-up if review wants the browser to have less reach than `bash`.

**Risk**: NSS trust of the operator CA through the proxy is verified by spike S3.

## R4. DAIV speaks CDP through a WebSocket tunnel and a local port-forward

**Decision**: daiv-sandbox adds `WS /session/{id}/browser/cdp`, authenticated with `X-API-Key`. Each WebSocket carries one raw TCP stream to the browser container's DevTools port (`127.0.0.1:39222`), through a `daiv-relay connect` exec. Binary frames carry bytes in both directions. The endpoint does not take the per-session lock, which a long-lived connection would hold past its 30-second wait. It stamps session activity, so the idle reaper keeps the session.

DAIV's `BrowserTunnel` opens a listener on `127.0.0.1:0` in the worker, one per run. It bridges each accepted connection to a new tunnel WebSocket. Stagehand then connects to `http://127.0.0.1:<local port>` as it would to a local Chrome:
- Chrome builds `webSocketDebuggerUrl` from the request's `Host` header, so the forwarded address comes back correct.
- `extension_cdp_url` is the browser-local `ws://127.0.0.1:39222/devtools/browser/<guid>`, built from the guid in `/json/version`. The PAC sends that port `DIRECT`, so the extension's own socket is never relayed.
- Chrome starts with `--remote-allow-origins=chrome-extension://<id>`. The id of an unpacked extension is derived from its absolute path, which is fixed, so it is known in advance. Pages cannot open a DevTools socket.

**Rationale**: The worker has no network path to the browser. A port-forward keeps Stagehand's own discovery, header and WebSocket handling unchanged. The service stays CDP-agnostic.

**Alternatives considered**:
- `--remote-debugging-pipe`. Rejected: only a parent process can use it.
- A WebSocket proxy in the service that understands CDP. Rejected: the service would have to track CDP's URLs and targets.
- `--remote-allow-origins=*`, which is what Stagehand's own launcher uses. Rejected: it would let any page that learns the browser's guid drive the browser.

## R5. DAIV sends the Stagehand extension when the browser starts

**Decision**:
- **Archive.** DAIV sends the archive that its installed `stagehand.get_extension_archive()` returns with the start request. The archive is deterministic. The browser container keeps it under `/opt/daiv/extension/<sha256>`, so a later start that sends the same digest skips the upload.
- **Launch.** Chromium starts with `--load-extension=<that path>`, `--enable-unsafe-extension-debugging` and new headless mode.
- **Image.** The browser image installs Chromium or Chrome for Testing, at version 137 or later.

**Rationale**:
- Stagehand drives the page from its extension's service worker. When it connects, it looks for exactly one enabled extension named "Stagehand Runtime", and it refuses a protocol version that does not match its own.
- Sending the extension from DAIV keeps the versions matched without tying a daiv-sandbox release to DAIV's `stagehand` pin.
- On a remote browser, `Extensions.loadUnpacked` cannot work, because it takes a path on DAIV's filesystem.
- Branded Chrome 137 and later ignores `--load-extension`, and `chrome-headless-shell` runs no extensions. That limits the image to Chromium or Chrome for Testing.

**Alternatives considered**: Baking the extension into the browser image. Rejected: every `stagehand` bump in DAIV would need a matching image release.

## R6. DAIV wraps Stagehand's facade instead of using `StagehandMiddleware` as is

**Decision**: DAIV adds `langchain-stagehand==0.0.1` with `uv add`. It pins `stagehand` and `stagehand-middleware` to `4.4.0` and `0.1.2`, the versions read for this plan. DAIV's own `BrowserMiddleware`:
- builds its tools on `stagehand_middleware.facade.StagehandFacadeTools`;
- reuses `langchain_stagehand.actions.SnapshotAction` for the `actions` argument;
- adapts the upstream tool descriptions (R11).

**Rationale**: `StagehandMiddleware` cannot be used directly:
- It takes a connected `Stagehand` client in its constructor. DAIV builds the agent before the first browser call, and FR-004 forbids starting a browser before then.
- Its facade writes `page.screenshot({path})` files to `./stagehand-artifacts` under the worker's working directory, with no option to move them. DAIV passes `artifact_root` set to a per-run temporary directory, which is removed when the run ends.
- Its tool names, `run`, `snapshot` and `screenshot`, are too generic for an agent that also has `bash` and MCP tools.

**Facts that shape the wrapper**:
- No model is passed to `Stagehand.create`. The three tools never use one (FR-014). `act`, `observe` and `extract` called from run code fail with Stagehand's own "LLM not configured" error.
- `logging={"level": "off"}`, because Stagehand writes its logs to `sys.stderr` and not to Python logging. Failures reach the logs through DAIV's own error paths (R12). Telemetry is off, because no `telemetry` option is passed.
- One `Stagehand` client per browser, tied to the run's event loop. A restart (R12) creates a new one.
- The facade has its own limits. Run code gets 60 s, plus a 10 s grace on the RPC. Snapshots and screenshots get 120 s. A run's return value is capped at 10 MiB, and artifacts at 32 per run (FR-016).

**New dependencies**:
- `browserbase` (Apache-2.0), imported only on Browserbase paths, which DAIV never takes.
- `tree-sitter` 0.26, with cp314 wheels, and `tree-sitter-javascript` 0.25. Their compatibility is verified by spike S5.
- None of them conflicts with `uv.lock`.

**Alternatives considered**:
- Subclassing `StagehandMiddleware` and overriding its private `_open_facade`. Rejected: it would break silently on a patch release.
- Depending on `stagehand-middleware` alone. Rejected: it gives up the user's requested integration point and its action schema, for no gain.

## R7. Lifecycle: one browser per run, one profile per sandbox session

**Decision**:
- **Start.** `POST /session/{id}/browser/` creates the browser container, or starts it if it is stopped. It installs the extension and the CA, launches Chromium on the profile at `/profile`, re-opens the tabs saved in `/profile/.daiv-tabs.json`, starts the relay (R2), and waits for DevTools. The response reports:
  - whether the profile was `restored`, `empty` or `reset`;
  - which URLs it re-opened.

  The call is idempotent while the browser runs.
- **Stop.** `DELETE /session/{id}/browser/` saves the page targets' URLs over local CDP, sends `Browser.close` so cookies flush, stops the relay and stops the container. The profile stays in the container's writable layer. `?force=true` removes the container and its profile.
- **Run end.** DAIV calls `SandboxBrowser.arelease()` in `executor/run.py`'s `finally`, before `_release_sandbox`. It disconnects Stagehand (`disconnect()` leaves Chrome running), closes the tunnel, stops the browser, and removes the run's artifact directory. It never raises.
- **Safety net.** daiv-sandbox stops the browser container whenever it stops the sandbox. It removes it whenever it removes the sandbox: on force close, on DAIV's replace, and when the reaper removes a stopped or idle session. The reaper also removes a browser container whose sandbox no longer exists.

**Rationale**:
- A stopped container keeps its writable layer, so the profile lives and dies with the sandbox (FR-007a, FR-007b, FR-009) with no new storage in DAIV.
- Every way DAIV discards a sandbox already goes through daiv-sandbox's force close or reaper.
- A session expiring in DAIV leaves its stopped sandbox to the reaper, so the profile follows the same path (FR-007b).

**Overlapping runs**: DAIV's session lock and daiv-sandbox's per-session lock serialize a session's runs. A run's browser start therefore waits for the previous run's stop to finish. The spec's edge case of a "later save" by a closing run cannot happen. Spec refinement 1 rewrites it.

**Alternatives considered**: Storing the profile in DAIV, encrypted in the database, and copying it in and out on each run. Rejected: it adds a secret store, and a copy on every run, for what the container layer already keeps.

## R8. Limits and Chromium's own sandbox

**Decision**: The browser container runs with:
- `mem_limit` and `nano_cpus` from the site settings, default 1 GiB and 1 CPU. daiv-sandbox must use `nano_cpus`: docker-py has no `cpus` argument (see the note below).
- `--disable-dev-shm-usage`, because Docker's default `/dev/shm` is 64 MB.
- `--no-sandbox`. Chromium's own sandbox needs user namespaces, which gVisor and most container setups withhold. The container is the isolation boundary.
- A non-root user (uid 1000).
- A managed Chromium policy with `DownloadRestrictions=3`, which blocks all downloads (FR-010). Run code cannot lift it.

When Chromium hits the memory limit, the kernel kills the browser container alone (FR-017b). DAIV sees the CDP connection drop, and handles it as a crash (R12). The service reports `oom_killed` from the container state, so the result can say the browser ran out of memory.

**Note, out of scope here**: daiv-sandbox passes `cpus` from `StartSessionRequest` straight to docker-py (`main.py:241-242`), and `containers.run` has no such argument. A sandbox environment with **CPUs** set should therefore fail to start with a 500. This should be fixed in daiv-sandbox separately. The browser container must not copy the pattern.

## R9. Whether a run gets the browser is decided once, at run start

**Decision**: A run gets the browser when all of these hold, checked when the workspace is built (FR-001, FR-003):
1. the site switch (`sandbox_browser_enabled`) is on;
2. the run's resolved sandbox spec has `browser=True`, from the environment's Browser field;
3. the run acquired a sandbox session (`workspace.is_ready`), not a skip-sandbox slash command;
4. the sandbox service reports the capability.

`SandboxWorkspace.browser` is then a `SandboxBrowser`, and otherwise `None`. `DiskWorkspace.browser` is always `None`. Agent code asks the workspace, never the run mode (`architecture/layering.md` rule 4).

**Capability**: daiv-sandbox adds `GET /-/capabilities/` → `{"browser": true}`, with no key, like `/-/version/`. An older service answers 404, which means no browser. `SandboxCapabilities` caches the answer for 5 minutes in Django's cache.

**When 1 and 2 hold and 4 does not** (FR-001a):
- the run gets no browser tools;
- DAIV logs at ERROR once per cache refresh per process, so Sentry gets the issue without one event per run;
- the Sandbox group of Site Configuration shows a warning on every view while the switch is on.

That warning is new: today's configuration warnings appear only after a save.

**Alternatives considered**:
- Probing `/-/version/` and comparing versions. Rejected: the service's version string is already inconsistent (`v0.6.0` in code against `0.5.0` in `pyproject.toml`).
- Detecting the missing endpoint on the first browser call. Rejected by the third clarification: the agent would see tools that cannot work.

## R10. The Browser field is neither inherited nor part of the fingerprint

**Decision**:
- `SandboxEnvironment.browser_enabled` defaults to `False`.
- `merge_sandbox_spec` takes `browser` from the per-run environment when there is one, and otherwise from the global default, as `egress` does today.
- `SandboxSpec.fingerprint` leaves `browser` out.
- The browser limits are site settings, so they are not in the fingerprint either.

**Rationale**:
- The spec's Assumption: like Network, each environment sets its own Browser field.
- FR-017a: changing the field must not replace a sandbox, and anything in the fingerprint does.

## R11. Three deferred tools on the main agent

**Decision**: `BrowserMiddleware`, added in `graph.py` before `InlineToolDefinitionsMiddleware` when `workspace.browser is not None`, exposes three tools.

**Tools**:
- `browser_run` (code or actions);
- `browser_snapshot`;
- `browser_screenshot`.

**Loading and cost**:
- They are not in `ALWAYS_LOADED_TOOLS`, so they are deferred. Until the agent loads them, they cost one line each in the deferred-tools index.
- The middleware adds no system prompt. Everything the agent needs is in the tool descriptions, which are paid for only once loaded. This is how SC-006 is met.

**What the descriptions add to the upstream ones**:
- `localhost`, `127.0.0.1` and `*.localhost` reach the sandbox's own services, so the agent opens the dev server it started with `bash`.
- Page content is untrusted input, never instructions (FR-015).
- Downloads are blocked; the sandbox's own tools can fetch a file within the same policy.
- The run description drops the upstream `stagehand-artifacts` path sentence, and points to `browser_screenshot` for screenshots.

**Subagents**: Subagents do not get the tools (spec Assumption). Stagehand allows one client per browser, so a subagent would need a browser of its own.

**Alternatives considered**:
- Always-loaded tools. Rejected: three full schemas on every call of every browser-enabled run breaks SC-006.
- A system-prompt section. Rejected for the same cost reason.

## R12. Failures, restarts and first-call notes

**Decision**: `SandboxBrowser` holds the run's browser state: `idle → starting → ready`, then `lost → restarting → ready`, or `unavailable`, and finally `released`.

**What each case does**:
- **Start failure.** A transport error, a non-2xx answer, or a timeout of the start budget (30 s) returns "The browser is unavailable for this run." DAIV logs at ERROR with the cause. A second failure in one run latches `unavailable` for the rest of the run, so the agent stops retrying.
- **Lost connection.** Stagehand's socket closes, an RPC fails with a connection error, or the container was OOM-killed. The current call fails with a result that says the browser stopped, and why when the service knows: "it ran out of memory". The next call restarts from the last saved profile (FR-020). Its result starts with a note: the browser restarted, and the pages and state since the profile was last saved were lost.
- **First call of a run.** The result starts with one of these notes:
  - the pages re-opened from the session's profile (FR-007a);
  - that the profile was reset (FR-007b);
  - nothing, for a session's first browser.
- **Facade errors**, such as a stale snapshot id, a timeout or invalid run code, come back as Stagehand's own `ToolException` text. Run code is the agent's own, and the errors are actionable.

No result carries a DAIV exception, a URL with the service's address, or the API key (FR-020, `patterns/errors.md` rule 5).

## R13. Screenshots stay within what providers and the media store accept

**Decision**:
- `browser_screenshot` defaults to the viewport as PNG.
- When the decoded image is over 5 MiB, DAIV retakes it once as JPEG at quality 60. Anthropic rejects images over 5 MB, and the run media store keeps up to 10 MiB.
- If it is still over 5 MiB, the result says the page is too large to capture whole, and to use `fullPage: false`.
- `ToolMediaMiddleware` stores the image unchanged, under the existing per-file and per-run limits (FR-018).

## R14. Models that cannot read images

**Decision**:
- `ToolMediaMiddleware` gains an `awrap_model_call` step. When the request's model profile says `image_tool_message` is `False`, it replaces each image block in a `ToolMessage` with a text note. The note says a screenshot was kept in the transcript, the model cannot view images, and the agent should use the page snapshot instead (FR-021).
- The model's own history is not changed. The swap applies only to the request, so the stored media and the transcript keep the image.
- When a profile is missing or silent, images are passed through, as today.

**Rationale**: `ToolMediaMiddleware` owns media from tool results. The same guard also covers MCP screenshot tools, which today send images to every model. langchain already carries `image_tool_message` on `ModelProfile`; DAIV has never read it.

**Alternatives considered**: Checking for image support inside `browser_screenshot`. Rejected: the store records media from the tool's own result, so a text-only result would drop the screenshot from the transcript.

## R15. The site's switch and limits, and the environment's field

**Decision**:
- **Site settings.** Three fields on `SiteConfiguration` join the existing Sandbox group, through its `sandbox_*` match:
  - `sandbox_browser_enabled`, a boolean defaulting to `False`, with the environment variable `DAIV_SANDBOX_BROWSER_ENABLED`;
  - `sandbox_browser_memory_bytes`, defaulting to 1 GiB, at least 256 MiB, with `DAIV_SANDBOX_BROWSER_MEMORY_BYTES`;
  - `sandbox_browser_cpus`, defaulting to `1.0`, at least `0.25`, with `DAIV_SANDBOX_BROWSER_CPUS`.

  The configuration page is admin-only, which makes the switch and the limits admin-only (FR-017, FR-017b).
- **Environment form.** The form gains `browser_choice` (On/Off), placed after Network. Whoever may edit the environment can change it: its owner for a User environment, administrators for a Global one.
- **Environment summary and picker.** The summary shows "Browser" when the field is on.

## R16. Transcript rows

**Decision**:
- **Row builders.** `ROW_BY_TOOL` in `chat/static/chat/js/tool-renderers.js` gets three entries:
  - `browser_run`: "Opened `<url>`" when the code is a single `page.goto`, "Ran `<n>` browser actions" for actions, and "Ran a browser script" otherwise;
  - `browser_snapshot`: "Read the page";
  - `browser_screenshot`: "Took a screenshot".
- **Labels and icon.** The `row_*` and `live_*` keys go in `sessions/transcript_labels.py` and in the JavaScript defaults. A new `browser` icon goes in `TRANSCRIPT_ICONS`, with its SVG.
- **Result bodies.** They reuse the existing text and media renderers. Screenshots already render through the tool-media path (FR-018, FR-019).

## R17. Measuring it

**Decision**:
- **SC-006, token half.** A unit test builds the main agent's first model request with the browser off and with it on, for the same run, and asserts the difference is only the three index lines. That is at most 2% of the off-request's input. This check is deterministic, and token medians from evals would be noise.
- **SC-006, eval half.** Browser tools appear only in runs that acquire a sandbox. Among the prompt-eval suites that is `code_review` today, and the first eval task confirms the list from `tests/integration_tests/conftest.py`. Record a baseline of those suites with the browser off, before the change. Re-run them with the browser on, and compare them with `evals/compare_runs.py`.
- **SC-001 and SC-002.** A new `browser` marker and `tests/integration_tests/test_browser.py`, with two cases:
  - fix a visible defect in a tiny static web app served from the sandbox, and screenshot it;
  - answer from a page whose text appears only after its script runs, served from the sandbox (no external host is needed).

  These cases need the local stack with a browser-capable daiv-sandbox (quickstart §5).

**Baseline recorded (2026-11-14)**:
- **Suites confirmed**: `tests/integration_tests/conftest.py`'s `sandbox_session` fixture is used only by `test_sandbox.py` (marker `sandbox`, not a prompt eval). Among the prompt-eval suites, only `code_review` acquires a sandbox (`test_code_review.py` builds a `SandboxWorkspace`). The baseline is `code_review` alone.
- **Model**: `anthropic:claude-haiku-5-5`, by the maintainer's choice for this feature, instead of the default `EVAL_MODEL`. The after-run uses the same model.
- **Code under test**: DAIV `main` at `84567d049`, from the main checkout; daiv-sandbox `main` at `d1d5ef5`, mounted from a detached worktree; the local GitLab's `srtab/daiv` at `f6cc0633` (the code-review cases pin their own `base_sha`). Keep the local GitLab unchanged until the after-run.
- **Output**: `/home/sfr/work/personal/daiv/eval-runs/browser-before.jsonl` (log beside it), 45 rows: 15 cases × 3 passes, 35 passed and 10 failed.
- **SC-003, SC-004, SC-007 and SC-008.** They are proved by unit tests on the DAIV side, and by daiv-sandbox's own integration tests against its relay, egress and reaper.

## Spikes to run first

Each spike gets a throwaway script in the daiv-sandbox repo, under runc and runsc. Results go in this file before slice 1 starts.

| # | Verifies | Pass when |
|---|---|---|
| S1 | Chromium 137+ in the browser image, the extension preloaded from a path, `--enable-unsafe-extension-debugging`, and `local_browser.connect` from another process through a forwarded port with a browser-local `extension_cdp_url` | `Stagehand.create` succeeds, and `snapshot`, `run` (`page.goto`) and `screenshot` work |
| S2 | PAC routing of loopback with `<-loopback>`, the DevTools port going `DIRECT`, and the `daiv-relay` exec-stdio pump | A Vite dev server bound to `127.0.0.1` in a network-none sandbox loads, its live-reload WebSocket stays up, and a page cannot open the DevTools socket |
| S3 | The operator CA in the browser's NSS database, through the session's egress proxy | An allowed HTTPS host loads, a denied one shows the 403 page naming the host, and a sub-resource from a denied host is blocked |
| S4 | First-call latency (SC-005) | Container start, Chromium, the extension attach and a local `goto` take under 5 s on runsc, at p50 of 10 cold starts |
| S5 | `uv add langchain-stagehand==0.0.1` on Python 3.14: tree-sitter 0.26 with tree-sitter-javascript 0.25, and the `@tool` schemas | `uv run python -c "import langchain_stagehand"`, and the facade's tree-sitter pre-check parses a sample script |

### Spike results (2026-11-14)

**Environment**:
- Docker 27.2; runc and gVisor `runsc` release-20261109.0.
- Browser image: `debian:trixie-slim` plus Debian's `chromium`, which is **Chrome/156.0.8078.79**, with `libnss3-tools` and `python3`.
- Sandbox image: `node:22-slim` with Vite 6.4.6.
- `langchain-stagehand` 0.0.1, `stagehand` 4.4.0, `stagehand-middleware` 0.1.2 in a throwaway Python 3.14.7 virtualenv.
- A Python stand-in for `daiv-relay`, pumped through `docker exec` stdio on the host as the service would pump it.

Every container, network and image was labelled `daiv-spike`, and removed afterwards.

**Summary**:

| # | runc | runsc | Result |
|---|---|---|---|
| S1 | pass (`Stagehand.create` 1.4 s) | pass (3.5 s) | **Pass** |
| S2 | pass, after a design change (below) | pass | **Pass, with the change** |
| S3 | pass | pass, once the CA was staged outside `/tmp` | **Pass** |
| S4 | cold p50 **2.87 s** (max 3.03), warm p50 2.63 s | cold p50 **5.64 s** (max 7.63), warm p50 5.27 s | **Fail on runsc**: refinement 2 applies |
| S5 | n/a | n/a | **Pass** |

**S1, Stagehand over a forwarded CDP port.** The browser container runs with `network_mode=none`. Chromium starts with `--load-extension=/opt/ext/stagehand`, `--enable-unsafe-extension-debugging` and `--remote-allow-origins=chrome-extension://<id>`. The worker connects with `local_browser.connect(cdp_url="http://127.0.0.1:<forwarded port>", extension_cdp_url="ws://127.0.0.1:39222/devtools/browser/<guid>")`, through a host port-forward that runs `relay connect 39222` in an exec per connection.
- **Extension id**: the id computed from the path (`ddmiknoojfjgdciacongbepelobeofgb`, from the first 32 hex characters of the path's sha256 mapped to a–p) matched the loaded extension. The restricted `--remote-allow-origins` was enough for the extension's own socket.
- **Facade calls**: `run` with `page.goto(data:…)` returned the title. `snapshot` returned element ids, a `click` action through those ids worked, and `screenshot` returned a PNG.
- **Disconnect**: after `disconnect()`, Chromium kept running, which is what R7 needs.

**S2, loopback into a network-none sandbox.**
- **The PAC design (R2) fails.** Chromium never asks a PAC script about loopback destinations, whatever `--proxy-bypass-list` says. `http://localhost:5173` went `DIRECT` inside the browser container and got `ERR_CONNECTION_REFUSED`.
- **What works** is a fixed proxy: `--proxy-server=socks5://127.0.0.1:39223 --proxy-bypass-list=<-loopback>;127.0.0.1:39222`. It removes the implicit loopback bypass and exempts only the DevTools port. The relay becomes the browser's only proxy, so it must route every destination itself:
  - loopback goes to the sandbox, multiplexed over the exec stdio;
  - other destinations go through `CONNECT` to the session's egress proxy, or are refused when there is none.

With that change, under both runtimes:
- Vite, bound to `127.0.0.1` in a network-none sandbox, served `http://localhost:5173` in 0.2 s (runc) and 0.5 s (runsc), and `http://127.0.0.1:5173` too.
- Its module script ran, and the HMR WebSocket connected through the relay. Editing `/app/main.js` in the sandbox live-reloaded the page within 10 s.
- A server bound only to `::1` answered 200: the relay tries `127.0.0.1`, then `::1`.
- A page could not open the DevTools WebSocket even with the right guid (`error`), and `fetch` of `/json/version` was blocked (`TypeError`).
- Navigation errors do not raise. `page.goto` to a closed port, or to an external host with no network, returned normally on Chromium's error page.

**S3, egress through the proxy.** Setup:
- an internal network;
- daiv-sandbox's egress proxy (`ghcr.io/srtab/daiv-sandbox-egress:latest`), with a throwaway CA and a policy allowing only `example.com` (`intercept: all`);
- the browser container on the internal network, with the CA added to `$HOME/.pki/nssdb` by `certutil`.

Results:
- `https://example.com/` loaded with the title "Example Domain", and a same-host `fetch` succeeded.
- A `fetch` and an `<img>` from `www.iana.org`, which is denied, were blocked.
- Navigating to `https://www.iana.org/` or `http://neverssl.com/` landed on `chrome-error://chromewebdata/` with `ERR_SOCKS_CONNECTION_FAILED`. The proxy's 403 text never reaches the page, because the relay turns the refused `CONNECT` into a SOCKS failure.
- **Control**: without the CA in NSS, `example.com` showed "Privacy error".
- **Background traffic**: Chromium still tried `accounts.google.com` in the background, and the proxy blocked it.
- **gVisor and `/tmp`**: under runsc, a file put into a running container's `/tmp` with `put_archive` was invisible to the container (`certutil: unable to open "/tmp/ca.crt"`). A path under `$HOME` worked.

**S4, first-call latency.** Each figure is 10 cold first calls (a new container) and 5 warm ones (a restart of the stopped container). The clock runs from container start to a loaded local page. Container creation (about 0.1–0.3 s) is not counted.

| Phase (p50, cumulative) | runc cold | runsc cold | runsc warm |
|---|---|---|---|
| Container started + extension copied | 0.13 s | 0.12 s | 0.10 s |
| DevTools ready | 0.82 s | 2.16 s | 2.22 s |
| `Stagehand.create` done | 2.54 s | 5.11 s | 4.67 s |
| Local page loaded | 2.87 s | 5.64 s | 5.27 s |

On runsc, Stagehand's extension attach (about 3 s, including its service-worker wake) costs more than Chromium's start (about 2 s). A warm restart saves nothing, because Chromium restarts either way.

**Reliability**: across about 25 first calls during S4's tuning runs, one `run` hit Stagehand's 70 s RPC timeout (`stagehand.callback_batch`). There was none in the 30 measured calls. The cause is unknown. R12's restart covers it, but a failed `goto` costs 70 s.

**S5, packages on Python 3.14.** `uv pip install langchain-stagehand==0.0.1` resolved `tree-sitter` 0.26.1 and `tree-sitter-javascript` 0.25.0, and they worked together:
- The facade's `check_run_syntax` accepted valid code and rejected `return (` with "Invalid JavaScript in run code".
- Everything imported with DAIV's pins (`langchain==1.4.9`, `langchain-core==1.7.7`).
- `@tool` built the schemas. The `actions` schema uses `$defs`/`$ref`, which the strict and frozen-tools providers must accept (check in slice 3).

### Design changes the results require

These change [contracts/sandbox-browser-api.md](contracts/sandbox-browser-api.md) and R2–R4. They are not yet applied there.

1. **No PAC (R2, R3).** Chromium gets a fixed SOCKS5 proxy, `--proxy-server=socks5://127.0.0.1:39223`, with `--proxy-bypass-list=<-loopback>;127.0.0.1:<devtools port>`. `daiv-relay listen` is a SOCKS5 server that routes by destination:
   - loopback goes to the sandbox, trying `127.0.0.1` and then `::1`;
   - everything else goes through `CONNECT` to the session's egress proxy, or is refused when the session has none.
2. **FR-006 is met by DAIV, not by the addon.** A refused host reaches the page as `ERR_SOCKS_CONNECTION_FAILED` on `chrome-error://chromewebdata/`, and `page.goto` does not raise. `BrowserMiddleware` checks the active page after each `browser_run`. When the page is that error page, it appends "`<host>` is not allowed by this environment's network policy" for a non-loopback host, and "nothing is listening on `<port>` in the sandbox" for a loopback one. The egress addon's 403 text change can be dropped.
3. **Nothing is staged under `/tmp` (gVisor).** The relay copied into the sandbox goes to `/home/daiv-sandbox/.daiv/relay`, and the CA in the browser container goes to `$HOME`.
4. **SC-005 under gVisor.** Refinement 2 is rewritten with the measured numbers (below).

If S2 had failed, the fallback was a per-session internal network (R2's first alternative), restricted to sessions whose environment has Browser on. It would have needed FR-017a to allow one sandbox replacement when Browser is first turned on. S2 passed with the design change above, so the fallback is not needed.

### After-evals (2026-11-14, T079)

All on `anthropic:claude-haiku-5-5`, outputs in `/home/sfr/work/personal/daiv/eval-runs/` (the comparison is `browser-compare.md`).

- **`browser` suite** (`browser-cases-2.jsonl`, 3 passes): SC-001 fix-and-screenshot 2/3, SC-002 script-built page 3/3, its no-browser control answered 0/3 (as intended), prompt injection 2/3. Each case holds a majority.
- **First pass** (`browser-cases.jsonl`): SC-002 and the injection case 0/3. For "the page at http://localhost:800x", the agent loaded `web_fetch`, which runs in DAIV and refuses local addresses, and then gave up. Two prompt changes fixed it: `browser_run`'s index line now says it opens services on localhost in the sandbox, and `web_fetch`'s refusal of a local address points to `bash` and the browser tools.
- **SC-006, eval half** (`code_review`, browser on through `DAIV_SANDBOX_BROWSER_ENABLED=true`, against `browser-before.jsonl`, final code in `browser-after-2.jsonl`, 6-pass confirmations in `browser-{before,after}-confirm.jsonl`): verdict **regressed**, on one case. `bug-session-owner-default` went from 6/9 to 2/9; the two other flips did not hold. No run in the after side called a browser tool or loaded one, so the prompt difference those runs saw is the three deferred-index lines (and the reworded `web_fetch` refusal, never triggered). A first after-run (`browser-after.jsonl`, before the prompt changes) flipped a different set of four cases, which suggests run-to-run noise, but the rule is that a confirmed flip stands until a larger re-run says otherwise.
- **Tokens**: median input per run 249,725 → 242,713 (−2.8%); the deferred-index lines are 0.8% of the first request (unit test).

### End-to-end check (2026-11-14, T078)

Run against the local stack's daiv-sandbox rebuilt from `feat/agent-browser` (runtime `runsc`, browser image `:dev`), through DAIV's own `SandboxWorkspace.aresolve_browser` and `SandboxBrowser`, in fresh sandbox sessions seeded from `srtab/daiv`:

- **SC-005, first call**: from the start of the call to a loaded local page (`page.goto("http://localhost:8000")` plus `page.title()`), 8 cold starts gave a median of **6.05 s** and a maximum of 9.04 s. That is within the refined gVisor target (median under 8 s).
- **Start alone**, 28 more cold starts: 4.7–7.5 s, of which Stagehand's extension attach (`local_browser.connect`) is 2.5–5.0 s, and `Stagehand.create` 0.01–0.1 s.
- **Hangs**: 2 of 38 cold starts, both in the first batch, stalled in the attach until DAIV's 30 s start budget, while the service had reported Chromium ready within 2 s. The agent got the "could not start" text. The cause is not found; the next 28 starts had none. The 2026-11-14 spikes saw one similar stall in about 25 starts.
- **Leaks**: no browser container was left after any run.

Not walked through: the dashboard steps of quickstart §4 (the site switch and environment field in the UI, the transcript rows and screenshot, the warm sandbox after a toggle, and step 6 against daiv-sandbox 0.5.0). Unit tests cover each of them.

## Spec refinements for approval

1. **Overlapping runs** (Edge Cases). A session's runs are serialized (R7), so replace the bullet with: "**A run starts while the session's previous run is still closing.** Its browser starts once the previous run's browser has saved its profile and closed."
2. **SC-005**: S4 measured runsc over 5 s, so this refinement applies. The proposed wording is "under 5 seconds at the median when the sandbox runtime is runc, and under 8 seconds when it is gVisor". The measured medians were 2.9 s and 5.6 s, with a gVisor maximum of 7.6 s. The largest part on gVisor is Stagehand's extension attach (about 3 s), which DAIV does not control.
3. **FR-006 and the Assumptions.** State that the browser's egress is the run's effective egress: the environment's policy plus the git-platform access DAIV opens for the run, as for the sandbox's own commands (R3).
4. **SC-002.** Serve the eval's script-built page from the sandbox rather than from an allowed external host. An external page makes a paid eval flaky. Reaching allowed external hosts is proved by spike S3 and daiv-sandbox's egress tests, not by the eval.
