# Contract: settings, forms and pages

## Site Configuration → Sandbox group

Three fields join the existing Sandbox group (`FieldGroup` match `sandbox_*`), after the existing fields:

| Label | Field | Help text |
|---|---|---|
| Browser | `sandbox_browser_enabled` | "Let the agent use a headless browser in sandbox environments that turn Browser on. Off turns it off everywhere." |
| Browser memory | `sandbox_browser_memory_bytes` | "Memory limit of each run's browser, separate from the environment's. At least 256 MiB." |
| Browser CPUs | `sandbox_browser_cpus` | "CPU limit of each run's browser, separate from the environment's. At least 0.25." |

**Locking**: an environment variable that is set locks its field, as for every site setting.

**Warning** (FR-001a, new): when `sandbox_browser_enabled` is on and `SandboxCapabilityProbe` reports `browser: false`, the Sandbox group's page shows an inline warning above the fields on every view, not only after a save:

> The sandbox service does not support the browser, so runs get no browser tools. Upgrade daiv-sandbox to 0.6.0 or
> later.

`SiteConfigurationGroupView` adds a `group_warnings` context entry for this. The probe's cached value keeps the page from waiting on the service.

## Sandbox environment form

**Browser field**: a radio after **Network**, rendered like it in `_form_body.html`:

| Field | Choices | Initial | Help |
|---|---|---|---|
| **Browser** (`browser_choice`) | **Off** (default), **On** | the instance's `browser_enabled` | "Lets the agent open pages in a headless browser beside the sandbox, when the site's Browser setting is on. It reaches services the agent starts in the sandbox, and only the hosts Network allows." |

**Note under On**: when the site switch is off, the radio shows "The site's Browser setting is off, so this has no effect yet." The radio is never disabled.

**Saving**: saving sets `browser_enabled`. Nothing else changes, and no sandbox is replaced (FR-017a).

**Summary and picker**: the environment list and the session environment picker append "· Browser" to `summary`.

## Run start (no UI)

`SandboxWorkspace` decides once, when it is built (R9). The run executor and chat streaming build it the same way, so jobs, schedules, webhooks, chat and MCP runs all behave alike.

## Documentation

| Page | Change |
|---|---|
| `docs/features/browser.md` | **New**. What the agent can do, local services and `localhost`, what it can reach (the environment's policy plus the git platform, R3), what carries over between a session's runs, limits and the ~300 MB footprint, the requirement of daiv-sandbox 0.6.0 or later, screenshots in the transcript, and how to turn it on. `description` front matter; added to `mkdocs.yml` `nav` and the `llmstxt` sections |
| `docs/features/sandbox-environments.md` | The **Browser** row in the field table, and a "Browser" section linking to the new page |
| `docs/reference/site-configuration.md` | The Sandbox group's new fields |
| `docs/reference/env-variables.md` | `DAIV_SANDBOX_BROWSER_ENABLED`, `DAIV_SANDBOX_BROWSER_MEMORY_BYTES`, `DAIV_SANDBOX_BROWSER_CPUS` |
| `docs/features/sessions.md` | "Images and files in tool results" mentions browser screenshots, and models that cannot read images |
| `CHANGELOG.md` | `Unreleased` → **Added**: "Add a headless browser the agent can use beside its sandbox, turned on per sandbox environment and site-wide (#PR)" |

There is no `UPGRADING.md` entry: nothing changes until an operator turns the browser on, and the docs state the daiv-sandbox requirement.

## daiv-sandbox documentation (in its repository)

- **README**: the browser image, `BROWSER_IMAGE`, the new endpoints, the relay, and the resource note.
- **CHANGELOG**: an `Added` entry, with a **Client note** that DAIV's browser needs this release.
