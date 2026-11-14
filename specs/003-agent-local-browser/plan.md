# Implementation Plan: Agent Local Browser

**Branch**: `claude/langchain-browserbase-integration-cc25e1` (spec-kit feature `003-agent-local-browser`) | **Date**: 2026-11-14 | **Spec**: [spec.md](spec.md) | **Status**: Approved by the maintainer on 2026-11-14, including the [spec refinements](research.md#spec-refinements-for-approval) and the daiv-sandbox 0.6.0 release it depends on

**Input**: Feature specification from `specs/003-agent-local-browser/spec.md`

## Summary

The agent gets three deferred tools, `browser_run`, `browser_snapshot` and `browser_screenshot`. They drive a headless Chromium that daiv-sandbox runs in a **browser container** of its own, beside the run's sandbox, with its own memory and CPU limits.

**Where the work lands**:
- **DAIV** wraps the facade of `langchain-stagehand` (Stagehand) in its own middleware. It starts the browser on the first call, reaches it through a WebSocket tunnel and a per-run local port-forward, and releases it when the run ends.
- **daiv-sandbox** gains the browser container, a capabilities endpoint, browser start and stop endpoints, a CDP tunnel, and `daiv-relay`. The relay carries the browser's loopback traffic into the sandbox, so `http://localhost:<port>` reaches the agent's dev server under runc and runsc alike. The browser's other traffic goes through the session's egress proxy.

**Session profile**: the browser profile (cookies, storage, open tabs) stays in the stopped browser container between a session's runs. It is removed whenever the sandbox is removed.

**Turning it on**: a run gets the browser only when all of these hold:
- the admin's site switch is on;
- the environment's Browser field is on;
- the sandbox service reports the capability.

When the switch and the field are on but the service lacks the capability, DAIV logs at ERROR and the configuration page warns.

The decisions behind each part are in [research.md](research.md) (R1–R17, spikes S1–S5).

## Technical Context

**Language/Version**: Python 3.14 (DAIV). daiv-sandbox is on Python 3.14 as well; the browser image is Debian slim with Chromium 137 or later. `daiv-relay` is a static Go binary.

**Primary Dependencies**:
- `langchain-stagehand==0.0.1` (new, MIT). It brings `stagehand==4.4.0`, `stagehand-middleware==0.1.2`, `browserbase` (Apache-2.0), `tree-sitter` and `tree-sitter-javascript`;
- Deep Agents on LangGraph (`create_daiv_agent`);
- `websockets` 17.4 (already locked) for the tunnel;
- Django 6, for the settings, form and configuration views.

**Storage**:
- **PostgreSQL**: one `AddField` on `SandboxEnvironment`, and three on `SiteConfiguration`.
- **Django cache**: the capability probe, kept for 300 s.
- **Not in DAIV**: the profile lives in daiv-sandbox's browser container.

**Testing**:
- pytest, pytest-django and pytest-asyncio (`asyncio_mode = "auto"`). The sandbox client, Stagehand and the WebSocket are stubbed, so no real browser runs in `make test`.
- JS labels go through `tests/unit_tests/sessions/test_transcript_labels.py`.
- daiv-sandbox's integration tests, under runc and runsc.
- Prompt evals: the new `browser` marker, plus the sandbox-using suites before and after.

**Target Platform**: Linux containers. DAIV's web and worker processes, and daiv-sandbox with Docker (runc or gVisor `runsc`).

**Project Type**: a Django web service with background workers running the agent, plus a separate code-execution service.

**Performance Goals**:
- The first browser call opens a local page in under 5 s (SC-005, spike S4).
- A run that never uses the browser pays only three deferred-index lines (SC-006).

**Constraints**:
- The browser runs only inside the sandbox's isolation and network boundary (FR-005, constitution I).
- No sandbox replacement on toggling (FR-017a).
- One Stagehand client per browser.
- Main agent only.
- Chromium 137 or later with extension support, so not branded Chrome and not `chrome-headless-shell` (R5).

**Scale/Scope**:
- One browser container per sandbox session that used the browser, about 300 MB each while running.
- Stopped between runs.
- Reaped with its sandbox: daiv-sandbox's 30 stopped sessions and 6 h grace.

**Unknowns**: none open in the design. Five assumptions only a running system can confirm are spikes S1–S5, run first (slice 0). A failed S2 sends the spec back (research R2).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | How the plan complies | Evidence to produce |
|---|---|---|
| I. Secure and isolated by default | See the list after this table | DAIV unit tests for the gating, and the error-text test. daiv-sandbox's integration tests in quickstart §3: refusals, DevTools origin, cross-session cookies, downloads |
| II. Enforced architecture boundaries | The tools come from a middleware registered in `graph.py` (layering rule 5). The browser is a `Workspace` member, `None` on disk runs, so agent code never branches on run mode (rule 4). New code sits in `core/sandbox` (client, schemas, tunnel, probe), `sandbox_envs` (field), `automation` (workspace browser, middleware) and `configuration` (warning). Every import goes downward, with no contract change and no `ignore_imports`. `SandboxBrowser`, `BrowserTunnel` and `SandboxCapabilityProbe` are one-concern classes | `make lint-imports`; `test_import_hygiene.py` unchanged |
| III. Platform and provider neutrality | Nothing touches the git platform API. No Stagehand model is configured, so the tools work with the run's model and any provider (FR-014). Image support is read from langchain's model profile, never from a model name. Every default (switch, memory, CPUs) is a site setting with an environment variable | Tests: no model passed to `Stagehand.create`; profile-driven image swap |
| IV. Tests prove project behaviour | Unit tests mirror each new or changed module (quickstart §2), with no real LLM, network or browser. daiv-sandbox's behaviour is proved by its own integration tests. Async database tests use `transaction=True` | `make test` output; daiv-sandbox test output |
| V. Measured agent behaviour | New tools and descriptions change what the model sees. The sandbox-using suites are measured before and after, plus a new `browser` suite with SC-001 and SC-002 (R17). The token half of SC-006 is a deterministic unit test | `compare_runs.py` output in the pull request |
| VI. Honest failure handling | Every browser failure reaches the agent as a fixed, actionable text (contracts/browser-tools.md) and the operator as ERROR. A degraded result is preferred: a restart note instead of failing the run, and a JPEG retake instead of dropping a screenshot. FR-001a's misconfiguration is visible on the configuration page, not only in logs | Tests per failure path; the ERROR cases below |
| VII. Documentation users can act on | A new `docs/features/browser.md`, plus updates to sandbox environments, site configuration, environment variables and sessions. The CHANGELOG and the glossary rows. Every procedure in the new page is run in quickstart §4 | Docs build (`mkdocs build --strict`); `test_env_variables_doc.py`; `test_docs_reference.py` |

**How Principle I is met**:
- **Fails closed.** A run gets the browser only when the site switch, the environment's field and the service capability all say yes. Anything missing means no tools (FR-001, FR-001a).
- **Isolation.** The browser runs in its own sandbox-runtime container: never in DAIV, and never with a path to DAIV's network.
- **Egress.** Every page request goes through the session's egress proxy, or nowhere (R3). The browser's effective egress equals the sandbox commands', including DAIV's runtime git rule. That adds no capability, and it is stated in the docs and in spec refinement 3.
- **DevTools.** The DevTools socket accepts only the extension's origin (R4).
- **Secrets.** No credential enters the browser (FR-008). The tunnel's key stays in DAIV's headers, and tool results carry fixed texts.

**ERROR-level cases**:
- a browser start that fails, with the cause;
- an OOM-killed browser;
- a release that fails (the container may leak until the reaper);
- the capability missing while the browser is on (once per cache refresh per process).

**Gates**:
- `make lint`, `make lint-typing` and `make test`.
- A docs build.
- Evals, per Principle V.
- A `CHANGELOG.md` `Unreleased` → **Added** entry.
- The glossary rows, classified MINOR.
- In daiv-sandbox, its own lint, tests, CHANGELOG entry and the browser image in the publish matrix.

**Result**: PASS. There are no exceptions to a MUST. Three deliberate choices carry real complexity, and are justified below.

**Post-design re-check (after Phase 1)**: PASS.
- The data model adds one environment column and three site settings, and stores no browser state in DAIV.
- The contracts add four daiv-sandbox endpoints and change no existing endpoint's shape.
- The new wire types join the schema-consistency test.

## Project Structure

### Documentation (this feature)

```text
specs/003-agent-local-browser/
├── spec.md
├── plan.md                         # this file
├── research.md                     # Phase 0: decisions R1–R17, spikes S1–S5, spec refinements
├── data-model.md                   # Phase 1: environment field, site settings, wire schemas, SandboxBrowser, browser container
├── contracts/
│   ├── sandbox-browser-api.md      # daiv-sandbox endpoints, behaviour changes, DAIV client methods, BrowserTunnel
│   ├── browser-tools.md            # tool names, args, descriptions, notes, failure texts, screenshots, transcript rows
│   └── settings-and-ui.md          # site settings, environment form, config warning, docs pages
├── quickstart.md                   # spikes, gates, unit and integration scenarios, manual run, evals
└── checklists/requirements.md
```

### Source Code

**DAIV** (repository root):

```text
daiv/
├── core/
│   ├── models.py                       # SiteConfiguration: sandbox_browser_{enabled,memory_bytes,cpus}
│   ├── migrations/                     # AddField × 3
│   ├── site_settings.py                # defaults + SiteSnapshot attributes
│   └── sandbox/
│       ├── schemas.py                  # SandboxCapabilities, BrowserStart{Request,Response}, BrowserStopResponse
│       ├── schemas.dump.json           # regenerated from daiv-sandbox
│       ├── client.py                   # get_capabilities, start_browser, stop_browser, browser_cdp_url, auth_headers
│       ├── capabilities.py             # NEW: SandboxCapabilityProbe (cached, ERROR once per refresh)
│       └── tunnel.py                   # NEW: BrowserTunnel (127.0.0.1 port-forward over the CDP WebSocket)
├── sandbox_envs/
│   ├── models.py                       # browser_enabled; summary
│   ├── migrations/                     # AddField
│   ├── spec.py                         # SandboxEnvOverride.browser, SandboxSpec.browser, merge rule; fingerprint unchanged
│   ├── services.py                     # row_to_override carries browser
│   ├── forms.py                        # browser_choice
│   └── templates/sandbox_envs/_form_body.html   # Browser radio + site-off note
├── automation/agent/
│   ├── workspace/
│   │   ├── base.py                     # Workspace.browser
│   │   ├── disk.py                     # browser = None
│   │   ├── sandbox.py                  # browser built per R9
│   │   └── browser.py                  # NEW: SandboxBrowser (lazy start, Stagehand connect, restart, notes, arelease)
│   ├── middlewares/
│   │   ├── browser.py                  # NEW: BrowserMiddleware (three tools, screenshot size rule)
│   │   └── tool_media.py               # awrap_model_call: image swap for models without image_tool_message
│   └── graph.py                        # add BrowserMiddleware when workspace.browser
├── sessions/
│   ├── executor/run.py                 # finally: await workspace.browser.arelease() before _release_sandbox
│   └── transcript_labels.py            # browser row/live/sum keys; "browser" icon
├── chat/static/chat/js/
│   ├── tool-renderers.js               # ROW_BY_TOOL entries
│   └── transcript-labels.js            # English defaults
├── core/static/core/img/icons/browser.svg   # NEW
├── configuration/
│   ├── views.py                        # group_warnings for the Sandbox group
│   └── templates/configuration/site_configuration_group.html   # inline warning block
└── mcp_api/server.py                   # get_environment: browser_enabled

tests/unit_tests/
├── core/sandbox/test_{client,capabilities,tunnel,schema_consistency}.py
├── core/test_site_settings.py
├── sandbox_envs/test_{spec,services,forms,models}.py
├── automation/agent/workspace/test_{browser,sandbox}.py
├── automation/agent/middlewares/test_{browser,tool_media}.py
├── automation/agent/test_graph.py
├── sessions/executor/test_run.py
├── sessions/test_transcript_labels.py
└── configuration/test_views.py
tests/integration_tests/test_browser.py         # NEW: SC-001, SC-002 (marker "browser")
docs/features/browser.md                        # NEW
docs/features/{sandbox-environments,sessions}.md, docs/reference/{site-configuration,env-variables}.md, mkdocs.yml
constitutions/glossary/README.md
CHANGELOG.md, pyproject.toml, uv.lock
```

**daiv-sandbox** (`/home/sfr/work/personal/daiv-sandbox`):

```text
daiv_sandbox/
├── main.py                 # /-/capabilities/, POST/DELETE /session/{id}/browser/, WS …/browser/cdp; stop/force close include the browser
├── schemas.py              # Browser* + SandboxCapabilities
├── browser.py              # NEW: BrowserManager (create/start/stop/remove, PAC, CA, extension, launch, readiness, tab save)
├── relay.py                # NEW: RelayPump (exec-stream byte pump per session)
├── reaper.py               # browser containers in removal + orphan sweep
├── egress/manager.py       # suspend/resume include the browser container
├── egress/addon.py         # 403 text names the host
└── config.py               # BROWSER_IMAGE, BROWSER_START_TIMEOUT_SECONDS
browser/                    # NEW: Dockerfile, launcher and tab-save scripts, managed policy, PAC template
relay/                      # NEW: daiv-relay (Go, static): listen | serve | connect
.github/workflows/docker.yml  # matrix: daiv-sandbox-browser
scripts/dump_schemas.py     # new types
tests/                      # unit + integration (runc and runsc)
README.md, CHANGELOG.md
```

**Structure Decision**:
- **DAIV.** The feature extends existing apps, and no app or layer is added. The browser's run lifetime belongs to the `Workspace`, which already owns the sandbox session. The tools follow the middleware extension point. Wire and transport code stays in `core/sandbox`, beside the client.
- **daiv-sandbox.** The browser gets a manager module beside `sessions.py` and `egress/`. The relay is a separate binary, so that a single static file serves every base image.

## Delivery Slices

Each slice can ship and be demonstrated on its own, in this order. Slices marked **sandbox** land in daiv-sandbox.

| # | Slice | Spec | Depends on |
|---|---|---|---|
| 0 | **Spikes S1–S5**, results recorded in research.md; the eval baseline recorded on `main` | R1–R6, SC-005, SC-006 | — |
| 1 | **sandbox**: the browser image, `daiv-relay`, `BrowserManager`, the start and stop endpoints, the CDP tunnel, `/-/capabilities/`, lifecycle with stop, force close, reaper and egress suspend, and the addon's 403 text. Integration tests under runc and runsc; release 0.6.0 | FR-005–FR-010, FR-017b, SC-003, SC-004, SC-008 | 0 |
| 2 | **Settings and gating**: the site settings and migration, the environment field, migration and form, the spec merge and fingerprint test, the capability probe, `Workspace.browser` (built but unused), the config warning | US2-1–4, US2-7; FR-001, FR-001a, FR-002, FR-003, FR-017, FR-017a | — (testable against a stubbed client) |
| 3 | **The browser in a run**: client methods and schemas, `BrowserTunnel`, `SandboxBrowser` (lazy start, connect, notes, restart, release), `BrowserMiddleware`, `graph.py`, the release in `run.py`, the screenshot rule | US1, US3, US4; FR-004, FR-007a, FR-011–FR-016, FR-020; SC-005, SC-007 | 2; 1 for the end-to-end run |
| 4 | **What people see**: transcript rows, labels and icon; the image swap for models that cannot read images | FR-018, FR-019, FR-021 | 3 |
| 5 | **Measurement**: the SC-006 token test, the `browser` eval suite (SC-001, SC-002), the after-run and comparison | SC-001, SC-002, SC-006 | 3, 0 |
| — | **Cross-cutting**: docs, glossary, CHANGELOG (DAIV and daiv-sandbox) | FR-022 | each slice |

**Which slices ship value**:
- Slice 2 ships alone as inert settings, because the tools only exist from slice 3.
- Slices 1 and 3 together are the MVP: User Story 1 works end to end.
- Slice 4 can follow in the same release.

## Complexity Tracking

| Choice | Why needed | Simpler alternative rejected because |
|---|---|---|
| A byte relay through daiv-sandbox (`daiv-relay`, exec streams, PAC) for the browser's loopback traffic | FR-005 needs the sandbox's `127.0.0.1` services under runsc and on network-none sandboxes, without replacing them (FR-017a) | Sharing the network namespace fails under runsc, where each container has its own gVisor network stack. A per-session internal network would mean replacing network-none sandboxes, and adds a bridge subnet per session against the known Docker address-pool limit (R2) |
| DAIV's own `BrowserMiddleware` over `StagehandFacadeTools` instead of `StagehandMiddleware` | `StagehandMiddleware` needs a connected client when the agent is built, contradicting FR-004. It writes run-code screenshots into the worker's working directory with no option, and its tool names collide in meaning with `bash` and MCP tools (R6) | Subclassing it means overriding the private `_open_facade`, which can break on any patch release |
| The Stagehand extension sent from DAIV on browser start, and cached by digest | The SDK refuses an extension of another protocol version, and a remote browser cannot load an extension from DAIV's filesystem (R5) | Baking the extension into the browser image would tie every DAIV `stagehand` bump to a daiv-sandbox image release |
