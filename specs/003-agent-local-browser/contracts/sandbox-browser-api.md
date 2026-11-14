# Contract: daiv-sandbox browser API

These are the additions to daiv-sandbox that DAIV depends on. They ship in a daiv-sandbox minor release, which this
document calls **0.6.0**. That release is the first with `/-/capabilities/` reporting `browser: true`. Every path is
under `/api/v1`, and every endpoint except `/-/capabilities/` needs `X-API-Key`. DAIV calls them through new
`DAIVSandboxClient` methods (signatures at the end).

## `GET /-/capabilities/` (no key)

```json
{"browser": true}
```

- **Absent on older services**: they answer 404, which DAIV reads as `{"browser": false}` (research R9).
- **When the service reports `false`**: when it cannot pull or find the browser image at startup. It logs why.

## `POST /session/{id}/browser/`

Starts the session's browser container, or creates it the first time. The request is multipart:

| Part | Content |
|---|---|
| `request` | JSON `BrowserStartRequest{memory_bytes, cpus, extension_sha256}` |
| `extension_archive` | the zip from `stagehand.get_extension_archive()`. DAIV may omit it after a start whose response showed the same `extension_sha256` already installed; the service answers 409 `extension missing` when it is absent and not cached |

**Steps** (under the per-session lock):
1. Resume the sandbox container, as for any session call.
2. Find the container labelled `daiv.sandbox.type=browser` and `daiv.sandbox.session=<id>`. If there is none, create it:
   - the runtime, user, limits and network are in [the data model](../data-model.md#browser-container-daiv-sandbox);
   - limits use `mem_limit` and `nano_cpus = int(cpus * 1e9)`.
3. Put the extension at `/opt/daiv/extension/<sha256>` if absent.
4. Start the container.
5. When the session has an egress proxy, stage the operator CA under the browser user's `$HOME` (never `/tmp`, which runsc hides from `put_archive`) and install it into the user's NSS database (`$HOME/.pki/nssdb`) with `certutil`.
6. Route the browser's traffic through `daiv-relay listen`, a SOCKS5 server on `127.0.0.1:39223` inside the browser container. Chromium uses it as its only proxy (step 8). The relay routes by destination:
   - loopback (`localhost`, `*.localhost`, `127.0.0.1`, `[::1]`) goes to the sandbox, multiplexed over the relay's stdio, where `daiv-relay serve` dials `127.0.0.1:<port>` and then `::1:<port>`;
   - everything else goes through an HTTP `CONNECT` to the session's egress proxy at its current address, or is refused with a SOCKS failure when the session has no proxy. A refused `CONNECT` is also a SOCKS failure.
7. Copy `daiv-relay` into the sandbox (`/home/daiv-sandbox/.daiv/relay`, mode 0755, sandbox uid). Start `daiv-relay serve` there and `daiv-relay listen` in the browser container as exec streams, and pump between them in a task the service tracks per session.
8. Launch Chromium, detached, with:
   - `--headless=new`, `--user-data-dir=/profile`, `--load-extension=<path>`, `--enable-unsafe-extension-debugging`;
   - `--remote-debugging-address=127.0.0.1`, `--remote-debugging-port=39222`, `--remote-allow-origins=chrome-extension://<id derived from path>`;
   - `--proxy-server=socks5://127.0.0.1:39223`, `--proxy-bypass-list=<-loopback>;127.0.0.1:39222`, `--disable-dev-shm-usage`, `--no-sandbox`. The bypass list removes Chromium's implicit loopback bypass and exempts only the DevTools port, so the extension's own socket is never relayed;
   - Stagehand's default flags (research R5 lists them);
   - the URLs in `/profile/.daiv-tabs.json`, if any.
9. Wait for `/json/version` on `127.0.0.1:39222`, at most 20 s.

**Response** `200 BrowserStartResponse`:

```json
{"started": true, "profile": "restored", "restored_urls": ["http://localhost:5173/login"],
 "devtools_port": 39222, "browser_ws_path": "/devtools/browser/4f1c…"}
```

| Field | Meaning |
|---|---|
| `started` | `false` when the browser was already running; the call is then a no-op that returns the current state |
| `profile` | `empty` on the container's first start. `reset` when the container existed but its profile could not be read, or it was recreated after a removal the service did not initiate, such as a missing container. `restored` otherwise |
| `restored_urls` | the tabs it re-opened |

**Errors**:

| Status | Reason |
|---|---|
| 404 | unknown session |
| 409 | extension missing; or the session is busy past the lock wait |
| 503 | the browser image is unavailable, or Chromium did not become ready in time |
| 500 | anything else, logged with the traceback |

Bodies carry a short reason, never a traceback.

## `DELETE /session/{id}/browser/?force=false`

**Default**:
1. Over local CDP, list the page targets.
2. Write their URLs to `/profile/.daiv-tabs.json`, in tab order, excluding `about:blank` and `chrome-extension://` pages.
3. Send `Browser.close`, wait up to 5 s for exit, then kill.
4. Stop the relay pump and both relay processes.
5. Stop the container. The profile stays.

**`force=true`**: remove the container, including its profile.

**Response**: `200 BrowserStopResponse{saved_urls, oom_killed}`. A session or browser that is already gone is not an error: the answer is `200` with empty values.

## `WS /session/{id}/browser/cdp`

- **Stream**: one raw TCP byte stream to `127.0.0.1:39222` inside the browser container, through `daiv-relay connect`. Binary frames only, in both directions; a text frame closes the socket with 1003.
- **Locking**: no per-session lock is held, and session activity is stamped every 30 s while open.
- **Closing**: either side's EOF closes the other. The service closes every tunnel of a session when it stops or removes the browser.
- **Errors**: 4404 for an unknown session or no running browser; 4403 for a wrong key, sent before accepting.

## Changes to existing behaviour

| Where | Change |
|---|---|
| `DELETE /session/{id}/` (stop) | Also stops the session's browser container, as browser stop does, before suspending the egress triad |
| `DELETE /session/{id}/?force=true`, the reaper's removal, the orphan sweep | Also removes the browser container. The orphan sweep removes a browser container whose `daiv.sandbox.session` names no existing container |
| Egress `suspend` / `resume` | Include the browser container among the network's members, so it is detached and reattached with the sandbox |
| `.github/workflows/docker.yml` | A matrix entry publishes `ghcr.io/srtab/daiv-sandbox-browser` |
| Settings | `BROWSER_IMAGE` (default the published image and tag), `BROWSER_START_TIMEOUT_SECONDS` (20) |

## DAIV client methods (`daiv/core/sandbox/client.py`)

```python
class DAIVSandboxClient:
    async def get_capabilities(self) -> SandboxCapabilities          # 404 → browser=False
    async def start_browser(self, session_id: str, request: BrowserStartRequest,
                            extension_archive: bytes | None) -> BrowserStartResponse
    async def stop_browser(self, session_id: str, *, force: bool = False) -> BrowserStopResponse
    def browser_cdp_url(self, session_id: str) -> str                 # ws(s)://…/session/{id}/browser/cdp
    def auth_headers(self) -> dict[str, str]                          # the X-API-Key header, for the tunnel
```

`BrowserTunnel` (`daiv/core/sandbox/tunnel.py`, new) is the worker-side port-forward:
- `aopen()` binds `127.0.0.1:0`. Each accepted connection opens one WebSocket to `browser_cdp_url` with `auth_headers()` and pumps bytes until either side closes.
- `http_url` is `http://127.0.0.1:<port>`.
- `aclose()` closes the listener and every open bridge.

It holds no state beyond the run.
