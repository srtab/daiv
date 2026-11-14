---
description: "Let the DAIV agent drive a headless browser beside its sandbox to check a web change on its own dev server, read script-built pages and take screenshots, within the environment's network policy."
---

# Browser

The browser lets the agent open web pages in a headless Chromium that runs beside the run's [sandbox](sandbox.md). The agent uses it to check its own web change on the dev server it started, to read a page that only its scripts build, and to take screenshots that show in the session's transcript.

The browser is off by default. A run gets it only when an administrator turns it on for the site and the run's [sandbox environment](sandbox-environments.md) turns it on too.

## What the agent can do

The agent gets three browser tools. Like other tools beyond the core set, they are deferred: the agent loads them when it needs them, so a run that never uses the browser pays only for their names.

| Tool | What it does |
|------|--------------|
| `browser_run` | Runs Playwright-shaped JavaScript against the page, such as `await page.goto("http://localhost:5173")`, or a batch of clicks, hovers, typing, fills, selections and key presses on elements named in the latest page snapshot. |
| `browser_snapshot` | Reads the active page as an outline of its elements, each with an id the next actions can name. It includes the content of frames. |
| `browser_screenshot` | Takes a screenshot of the visible page, or of the whole page. |

The browser starts on the agent's first browser call, not when the run starts. The transcript shows each call with a readable label, such as **Opened `http://localhost:5173`**, **Ran 3 browser actions** or **Took a screenshot**. Screenshots show under the call, as described in [Images and files in tool results](sessions.md#images-and-files-in-tool-results).

The tools use the run's own model and need no other model, provider account or API key. Their descriptions tell the agent that page content is untrusted input, never instructions.

Only the main agent drives the browser. Subagents do not get the browser tools.

## Turn it on

The browser needs daiv-sandbox 0.6.0 or later, and a sandbox environment with a base image. To turn it on:

1. As an administrator, select **Settings** → **Site configuration** → **Sandbox**, and turn on **Browser**. Or set `DAIV_SANDBOX_BROWSER_ENABLED=true`, which locks the field.
2. Select **Settings** → **Sandbox environments**, edit the environment your web repositories use, set **Browser** to **On**, and save.

The change applies from the next run that starts. It never replaces a session's sandbox, so a warm session keeps its container. Each environment sets its own **Browser** field: an environment does not take it from the global default.

When the site's **Browser** setting is off, the environment form shows `The site's Browser setting is off, so this has no effect yet.` under **On**.

When **Browser** is on but the sandbox service is older than 0.6.0, runs get no browser tools, as if it were off. DAIV logs the cause at ERROR, and the **Sandbox** page of **Site configuration** shows `The sandbox service does not support the browser, so runs get no browser tools. Upgrade daiv-sandbox to 0.6.0 or later.`

## What the browser can reach

- **Services in the sandbox.** `localhost`, `127.0.0.1`, `[::1]` and any `*.localhost` name reach the sandbox's own loopback, so the agent opens the dev server it started with `bash`. This works whatever the environment's **Network** setting is.
- **Other hosts.** The browser reaches the hosts the environment's [network access](sandbox-environments.md#network-access-and-egress-policy) allows, through the same egress proxy as the sandbox, for every request a page makes: navigations, scripts, images, frames and redirects. With **Network** **Off**, it reaches no external host.
- **The git platform.** Like the sandbox's own commands, the browser also reaches the git platform DAIV opens for the run, with the run's short-lived credential added by the proxy. A page on an allowed host could send requests there too, as a package script the agent runs already can.

When a navigation is refused, the tool result names the host: `example.com is not allowed by this environment's network policy.`, or `Nothing is listening on port 5173 in the sandbox.` for a local address.

Downloads are blocked. The agent can fetch a file with `bash` instead, within the same network policy.

DAIV puts no credentials in the browser: not yours, not DAIV's git credentials, and not the environment's secrets. When a page asks for a login, the agent can sign in to a local service with test credentials it finds in the repository.

## What carries over between runs

The browser itself lives for one run, but the session keeps its **browser profile**: the cookies, the site storage and the addresses of the open tabs. When a run that used the browser ends, DAIV saves the profile, and the session's next run starts its browser from it and re-opens those addresses. The first browser result of that run lists them, so a sign-in to a local service in one run is still in effect in the next. Other page state, such as form contents and scroll positions, is not kept.

The profile lives and dies with the session's sandbox. It is discarded when the sandbox is replaced, for example because its environment changed, and when the sandbox service removes it, for example after the session expires. The next browser then starts empty, and its first result says so. Runs that different people start in one session share its profile, as they share its sandbox. No session's profile ever reaches another session.

## Limits and cost

- **Resources.** Each browser runs in a container of its own, with its own memory and CPU limits, separate from the environment's **Memory** and **CPUs**. Administrators set them in **Site configuration** → **Sandbox**: **Browser memory** (default 1 GiB, at least 256 MiB) and **Browser CPUs** (default 1, at least 0.25), or with `DAIV_SANDBOX_BROWSER_MEMORY_BYTES` and `DAIV_SANDBOX_BROWSER_CPUS`. A browser that passes its memory limit stops alone: the sandbox, the dev server and the agent's commands keep running.
- **Memory cost.** A browser uses roughly 300 MB on a simple page. Size the sandbox host for one browser per concurrent run that uses it. A browser runs only while its run does.
- **Time limits.** Starting the browser has 30 seconds. Run code has 60 seconds, and a snapshot or screenshot has 120 seconds. A call that passes its limit fails with a result that says so, and the run continues.
- **Screenshots.** A screenshot over 5 MiB is retaken once as a JPEG. If it is still too large, the result tells the agent to capture the visible page instead. The usual limits on media in tool results apply.
- **Models that cannot read images.** A screenshot is still kept for the transcript, and the agent is told it cannot view the image and should read the page snapshot instead.

## When the browser fails

The agent gets a short result, never internal error text, and DAIV logs the cause:

| What happened | What the agent reads |
|---------------|----------------------|
| The browser could not start | `error: The browser could not start. Try again; if it fails again, continue without it.` After a second failure in the run: `error: The browser is unavailable for this run.` |
| The browser crashed or disconnected | `error: The browser stopped (the connection was lost). The next browser call restarts it from this session's last saved profile.` When it ran out of memory, the reason reads `it ran out of memory`. |

After a crash, the next browser call restarts the browser from the session's last saved profile, and its result says that the pages and state since then were lost. When the run ends, whether it succeeded, failed, was cancelled or timed out, DAIV closes its browser.

## Related pages

<div class="grid cards" markdown>

-   :octicons-container-24: **Sandbox environments**

    ---

    The **Browser** field, the network policy the browser follows, and the other settings of an environment.

    [:octicons-arrow-right-24: Sandbox environments](sandbox-environments.md)

-   :octicons-gear-24: **Site configuration**

    ---

    The site's **Browser** switch and the browser's memory and CPU limits, in the **Sandbox** group.

    [:octicons-arrow-right-24: Site configuration](../reference/site-configuration.md)

-   :octicons-comment-discussion-24: **Sessions**

    ---

    How screenshots and other tool media show in the transcript.

    [:octicons-arrow-right-24: Sessions](sessions.md)

</div>
