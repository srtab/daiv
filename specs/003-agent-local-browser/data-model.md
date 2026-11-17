# Data Model: Agent Local Browser

The feature adds two DAIV migrations:
- a column on `SandboxEnvironment`;
- three fields on `SiteConfiguration`.

DAIV stores no browser state, and no `Run` or `Session` field is added. The browser profile lives in daiv-sandbox's browser container (research R7). The run's browser state is held in memory by `SandboxBrowser`, for the length of the run.

## SandboxEnvironment (DAIV, `sandbox_envs`)

| Field | Type | Default | Rules |
|---|---|---|---|
| `browser_enabled` | `BooleanField` | `False` | The environment's Browser field (FR-017). Not inherited from the global default (R10). Changing it never replaces a sandbox (FR-017a) |

- **Migration**: `AddField` with `default=False`. It is deploy-safe, and existing rows read as Browser off (FR-002).
- **Summary**: `summary` and `short_summary` add "Browser" when the field is on. `humanise_global_default()` leaves it out, as it does Network.
- **MCP**: `get_environment` in `mcp_api/server.py` includes `browser_enabled` read-only, beside `is_networked`.

## SandboxEnvOverride and SandboxSpec (DAIV, `sandbox_envs/spec.py`)

```text
SandboxEnvOverride
  + browser: bool | None        # None when the override comes from no environment row

SandboxSpec
  + browser: bool = False       # excluded from fingerprint
```

- **`merge_sandbox_spec`**: `browser = per_run.browser` when a per-run environment exists, else the global default's. This is the same rule as `egress`.
- **`fingerprint`**: unchanged payload, `{base_image, memory_bytes, cpus, env_vars, egress.policy}`. A test asserts that two specs differing only in `browser` have the same fingerprint.

## SiteConfiguration (DAIV, `core`)

| Field | Type | Default | Env var | Validation |
|---|---|---|---|---|
| `sandbox_browser_enabled` | `BooleanField(null=True)` | `False` | `DAIV_SANDBOX_BROWSER_ENABLED` | none |
| `sandbox_browser_memory_bytes` | `PositiveBigIntegerField(null=True)` | `1073741824` (1 GiB) | `DAIV_SANDBOX_BROWSER_MEMORY_BYTES` | at least `268435456` (256 MiB) |
| `sandbox_browser_cpus` | `FloatField(null=True)` | `1.0` | `DAIV_SANDBOX_BROWSER_CPUS` | at least `0.25` |

- **Where they are declared**: the defaults in `_build_field_defaults()`, with typed attributes on `SiteSnapshot`. The fields are added to `SiteConfigurationForm.Meta.fields`.
- **Group**: they join the Sandbox `FieldGroup` through `sandbox_*`.
- **Docs**: they are documented in `docs/reference/env-variables.md`, which `test_env_variables_doc.py` enforces.
- **Migration**: one `AddField` × 3, all nullable. Null means the default.

## SandboxCapabilities (DAIV, `core/sandbox`)

```text
SandboxCapabilities            # wire schema of GET /-/capabilities/ (contracts/sandbox-browser-api.md)
  browser: bool = False

SandboxCapabilityProbe         # class; one per process
  async def aget(client) -> SandboxCapabilities   # Django cache, key "sandbox:capabilities", 300 s
                                                  # 404 → SandboxCapabilities(browser=False)
                                                  # transport error → browser=False, logger.error once per refresh
```

## Wire schemas (shared with daiv-sandbox)

These are added to `daiv/core/sandbox/schemas.py` and to the dump. `test_schema_consistency.py` lists them in `_SHARED_TYPES`. The full shapes are in [contracts/sandbox-browser-api.md](contracts/sandbox-browser-api.md).

```text
BrowserStartRequest
  memory_bytes: int            # from sandbox_browser_memory_bytes
  cpus: float                  # from sandbox_browser_cpus; daiv-sandbox maps to nano_cpus
  extension_sha256: str        # digest of the Stagehand extension archive sent with the request

BrowserStartResponse
  started: bool                # false when it was already running (idempotent start)
  profile: "empty" | "restored" | "reset"
  restored_urls: list[str]     # tabs re-opened from the saved profile, in tab order
  devtools_port: int           # the browser-local DevTools port, for extension_cdp_url
  browser_ws_path: str         # "/devtools/browser/<guid>"

BrowserStopResponse
  saved_urls: list[str]
  oom_killed: bool             # the container had been killed for memory before this stop
```

## SandboxBrowser (DAIV, `automation/agent/workspace/browser.py`, in memory)

One per run that has the browser (R9). Built by `SandboxWorkspace`, and released in `executor/run.py`'s `finally`.

```text
state: idle → starting → ready ─┬→ released
                 │              ├→ lost → restarting → ready
                 └→ unavailable └→ (2nd start failure) unavailable
```

| Attribute | Meaning |
|---|---|
| `session` | the run's `SandboxSession`; `session_id` is read when the browser starts |
| `limits` | `(memory_bytes, cpus)`, snapshotted at run start (FR-003) |
| `tunnel` | `BrowserTunnel` while `ready` |
| `stagehand` | the `Stagehand` client while `ready`; a restart replaces it |
| `facade` | `StagehandFacadeTools` with `artifact_root` set to the run's temporary directory |
| `pending_note` | the note the next tool result starts with: re-opened pages, profile reset, or restarted after a loss |
| `start_failures` | 0–2; at 2 the state latches to `unavailable` |

**Transitions**:
- `idle → starting` happens on the first tool call (FR-004).
- `ready → lost` happens on a closed CDP socket, or an RPC connection error.
- `arelease()` can run from any state. It is idempotent and never raises; it logs at ERROR.

## Browser container (daiv-sandbox)

| Aspect | Value |
|---|---|
| Labels | `daiv.sandbox.type=browser`, `daiv.sandbox.session=<sandbox container id>`, plus `daiv.sandbox.egress=<token>` when on the session's egress network |
| Image | `ghcr.io/srtab/daiv-sandbox-browser` (Chromium ≥ 137, `daiv-relay`, `certutil`, the managed policy blocking downloads) |
| Runtime and user | the service's `RUNTIME`; uid 1000 |
| Limits | `mem_limit`, `nano_cpus` from `BrowserStartRequest` |
| Network | the session's `daiv-egress-<token>` network when the session has an egress proxy, else `none` |
| Filesystem | `/profile`: the Chromium user data dir and `.daiv-tabs.json`, kept across stop and start. `/opt/daiv/extension/<sha256>`: the extension |
| Lifetime | created on the first start; stopped by browser stop and by sandbox stop; removed by force close, replace, the reaper, and the orphan sweep |

**Sandbox-side state**: `/home/daiv-sandbox/.daiv/relay`, the `daiv-relay` binary copied in when the browser starts. It is the only change to the sandbox container's filesystem.

## Glossary

`constitutions/glossary/README.md` gains two rows, after "Egress policy". This is a companion change, classified MINOR.

| Term | Meaning | Not |
| --- | --- | --- |
| Browser | The headless browser DAIV starts for a run, beside its sandbox session, and drives with the browser tools (`SandboxBrowser`) | Browserbase, the user's browser |
| Browser profile | What a session's browser keeps from one run to the next: its cookies, site storage and open tab addresses | browser state, session cookies |

"Browser container" stays daiv-sandbox's word and is not a glossary term, as the sandbox's own container is not.
