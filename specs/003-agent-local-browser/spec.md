# Feature Specification: Agent Local Browser

**Feature Branch**: `claude/langchain-browserbase-integration-cc25e1`

**Created**: 2026-11-14

**Status**: Draft

**Input**: User description: "Integrate https://github.com/langchain-ai/langchain-browserbase to allow DAIV using a local browser". The research behind this draft found:

- The repository publishes the package `langchain-stagehand`. It is one agent middleware with three browser tools: run a sequence of page actions, read a snapshot of the page with element ids, and take a screenshot. These three tools need no model of their own.
- It drives either a browser hosted by Browserbase or a local Chrome, launched by the library or reached over the browser's debugging protocol. A local browser needs no Browserbase account.
- Its page actions are "not a general-purpose security sandbox or a substitute for network isolation". Isolation and network control are left to the application.
- A spike on 2026-10-29 found that headless Chromium runs in a sandbox-shaped container under gVisor with no network. It is ready in under a second, uses about 300 MB of memory, and can be reached through the sandbox's exec channel.

## Terms

- **Browser**: a headless web browser that DAIV starts for one run and drives for the agent. It is never the person's own browser.
- **Browser tools**: what the agent uses to drive the browser: run a sequence of page actions, read a page snapshot, take a screenshot.
- **Page snapshot**: a text outline of the page's elements. Each element has an id that the agent's next actions can name.
- **Local service**: a program the agent started in the run's sandbox, such as the repository's dev server, reached on the sandbox's own addresses.
- **Browser profile**: what a session's browser keeps from one run to the next: its cookies, its site storage and the addresses of its open tabs.

## Clarifications

### Session 2026-11-14

- Q: Where does the browser run: beside the run's sandbox, in DAIV's own worker, or as a browser service the operator runs? → A: Beside the run's sandbox, inside its isolation and network boundary. The browser reaches the sandbox's local services and only the hosts the environment's egress policy allows. DAIV provides the browser, so it works with any base image. Recorded in FR-005, FR-006 and the Assumptions.
- Q: At what scope is the browser turned on: one site-wide switch, a field on each sandbox environment, or both? → A: Both. Each sandbox environment has a Browser field, like Network, set by whoever manages the environment. A site-wide switch that only administrators control sits above every environment. A run gets the browser only when both are on. Recorded in FR-017, FR-017a and User Story 2.
- Q: Should anything the browser holds carry over between a session's runs, or should every run start with an empty browser? → A: The session keeps the browser profile. Cookies, site storage and the addresses of open tabs are saved when a run ends, and restored on the session's next run, as the sandbox keeps its files. Runs that different people start in one session share it. Recorded in Terms, FR-007 to FR-007b, FR-008, FR-020, User Stories 1 and 2, the Edge Cases, Key Entities, SC-008 and the Assumptions.
- Q: Should the browser get its own memory and CPU limits, or share the sandbox environment's? → A: Its own limits, set by administrators for the whole site, defaulting to 1 GiB and 1 CPU. The environment's Memory and CPUs keep applying to the agent's commands, and a browser that runs out of memory crashes alone. Recorded in FR-017b, FR-022, the Edge Cases, Key Entities and the Assumptions.
- Q: When the browser is turned on but the sandbox service is too old to run browsers, what happens? → A: Runs are offered no browser tools, as if the browser were off. DAIV logs the cause at ERROR, and the site configuration page warns that the sandbox service needs upgrading. Recorded in FR-001a, User Story 2, the Edge Cases and the Assumptions.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The agent checks its own web change in a real browser (Priority: P1)

A person asks DAIV to fix a layout bug in a web application's sign-up form. The agent changes the code and starts the application's dev server in its sandbox. It then opens the form in the browser, fills it in and submits it, and takes a screenshot of the result. The screenshot shows the fixed form, and the agent's report includes it. The person sees the screenshot in the session's transcript next to the call that took it.

**Why this priority**: Today the agent can change a web front end but cannot see it. Its only check is to read the HTML or run unit tests, so layout, script and interaction bugs go unverified. The person finds out by checking out the branch. This is the reason to add a browser.

**Independent Test**: In a session on a small web application with a visible defect, ask the agent to fix the defect and show the result. The transcript shows the agent starting the dev server, opening the page, interacting with it and taking a screenshot, and the screenshot shows the fixed state.

**Acceptance Scenarios**:

1. **Given** a run whose sandbox serves the application on a local port, **When** the agent opens that address in the browser, **Then** the page loads and the agent can read its page snapshot.
2. **Given** an open page, **When** the agent clicks, types, fills fields, selects options or presses keys on elements named in its latest snapshot, **Then** the actions happen in order, and the tool result reports the outcome of each.
3. **Given** an open page, **When** the agent takes a screenshot, **Then** the agent sees it as an image, and the transcript shows it with the call that took it.
4. **Given** the page has changed since the agent's last snapshot, **When** the agent acts on an element id from that snapshot that no longer exists, **Then** the action fails with a result that says to take a new snapshot, and the run continues.
5. **Given** the agent signed in to a local service in the session's previous run, **When** the next run opens the browser, **Then** it is still signed in, the pages that were open are open again, and the first browser call's result lists them.

---

### User Story 2 - Operators decide whether the browser exists and what it can reach (Priority: P1)

An administrator turns the browser on for the site, then turns it on in the sandbox environment their front-end repositories use. From then on, runs in that environment are offered the browser tools. The browser reaches the run's own local services, and only the external hosts that the environment allows. Runs in other environments, runs without a sandbox, and every run while the site switch is off behave exactly as today.

**Why this priority**: A browser loads pages and runs their scripts on DAIV's behalf, inside runs that hold people's repositories and credentials. Without these controls, adding one would open a path around the sandbox's network rules. DAIV's first principle is that every path fails closed, so the browser cannot ship without them.

**Independent Test**: With the site switch off, start a run in an environment with Browser on, and confirm it offers no browser tools. Turn the site switch on. In a run whose environment has Browser on and network off, confirm the agent can open a local service and cannot open any external host. Allow one host in the environment's egress policy, and confirm the agent can open that host and no other. Confirm a run in an environment with Browser off offers no browser tools.

**Acceptance Scenarios**:

1. **Given** a fresh or upgraded install, **When** a run starts, **Then** the agent is offered no browser tools.
2. **Given** the site switch is on, **When** a run starts without a sandbox, or in an environment with Browser off, **Then** the agent is offered no browser tools.
3. **Given** an environment with Browser on, **When** the site switch is off and a run starts in that environment, **Then** the agent is offered no browser tools.
4. **Given** the browser is on and the sandbox service cannot run browsers, **When** a run starts, **Then** the agent is offered no browser tools, DAIV logs the cause at ERROR, and the site configuration page warns that the sandbox service needs upgrading.
5. **Given** the browser is on and the run's sandbox environment has network off, **When** the agent opens an external address, **Then** the page does not load, and the tool result names the refused host.
6. **Given** an egress policy that allows one host, **When** a page the agent opened loads scripts, images or frames from other hosts, **Then** those requests are refused, and the allowed host's content still loads.
7. **Given** a session whose sandbox is running, **When** someone turns Browser on or off in its environment, **Then** the change applies from the session's next run, and the session's sandbox is not replaced.
8. **Given** two runs with the browser on in different sessions, **When** the second run opens the browser, **Then** it has no cookies, storage, history or signed-in state from the first.
9. **Given** a session's first run, **When** it opens the browser, **Then** the browser starts empty.

---

### User Story 3 - The agent reads pages that need a real browser (Priority: P2)

A person asks the agent how a library's API works. The documentation site builds its pages with scripts, so the page fetch tool returns an empty shell. The site is on the environment's egress allow-list. The agent opens the page in the browser, reads its snapshot, and answers from what the page shows.

**Why this priority**: The page fetch tool covers static pages. Script-built documentation, dashboards and single-page applications return nothing useful to it. The browser closes that gap, but only within the reach operators already allowed, so it ranks below the agent checking its own work.

**Independent Test**: Allow a host that serves a page whose content appears only after its scripts run. Ask a question that only that content answers. The agent opens the page in the browser and answers correctly.

**Acceptance Scenarios**:

1. **Given** an allowed page whose content is built by its scripts, **When** the agent reads its page snapshot, **Then** the snapshot contains the content the scripts built.
2. **Given** a page whose text tells the agent to take an action, **When** the agent reads it, **Then** the agent treats the text as the page's content, not as instructions, as the browser tools' descriptions say.

---

### User Story 4 - The browser fails honestly and never outlives its run (Priority: P2)

The browser crashes partway through a run, or a page never finishes loading. The agent's tool call returns a result that says what happened, and the run continues. The agent's next browser call starts a fresh browser from the session's last saved profile, and the agent is told which page state was lost. When the run ends, its browser is closed, whether the run succeeded, failed, was cancelled or timed out. The operator finds the cause in DAIV's logs.

**Why this priority**: A browser is a large process, at roughly 300 MB each. One that outlives its run, or one that hangs a run, costs the host memory and the person their run. The rest of the feature depends on failures being visible and bounded.

**Independent Test**: Kill the browser mid-run. The next browser call reports the loss, starts a fresh browser and succeeds. Cancel a run while its browser is open. No browser from that run is still running a minute later.

**Acceptance Scenarios**:

1. **Given** a browser that crashed or disconnected, **When** the agent makes its next browser call, **Then** the result says the browser restarted from the session's last saved profile and the page state since then was lost, and the call runs on the fresh browser.
2. **Given** a page action that does not finish within its time limit, **When** the limit passes, **Then** the call fails with a result that says it timed out, and the run continues.
3. **Given** a run with an open browser, **When** the run succeeds, fails, is cancelled or times out, **Then** its browser is closed.
4. **Given** the browser cannot start, **When** the agent calls a browser tool, **Then** the result says the browser is unavailable for this run, without internal error text, and DAIV logs the cause at ERROR.

---

### Edge Cases

- **The run's model cannot read images.** A screenshot is still kept for people in the transcript. The agent is told it cannot view the image and should use the page snapshot instead.
- **Screenshots past the run's media limits.** The existing limits on images and files in tool results apply. A screenshot over a limit is not kept, and the agent and transcript say why, as for any other tool result media.
- **The page offers a file download.** Downloads are refused. The agent is told it can fetch the file with the sandbox's own tools, within the same egress policy.
- **A page opens a pop-up or a new tab.** The agent can see it and switch to it. Every tab closes with the browser.
- **The page asks for a login.** DAIV supplies no credentials. The agent can sign in to a local service with test credentials it finds in the repository, and is told nothing else.
- **The sandbox is replaced between a session's turns**, for example because its environment changed. The browser profile is discarded with it. The next run starts an empty browser, and its first browser call's result says so.
- **A run starts while the session's previous run is still closing.** Its browser starts once the previous run's browser has saved its profile and closed.
- **Different people start runs in one session**, for example a mention on a merge request the session owns. Their runs share the session's browser profile, as they share its sandbox.
- **The profile cannot be saved when a run ends**, for example because the browser crashed. The next run starts from the last profile that was saved, or empty if there is none, and its first browser call's result says so.
- **A page uses more memory than the browser's limit.** The browser crashes alone, and the sandbox, the dev server and the agent's commands keep running. The call fails as for any crash (FR-020), and the result says the browser ran out of memory.
- **The agent never uses the browser.** No browser is started, the session's profile stays as it was, and the run costs nothing beyond the browser tools' descriptions.
- **DAIV is upgraded before the sandbox service.** Runs get no browser tools until the sandbox service supports browsers (FR-001a), and the agent never sees a tool that cannot work.
- **The browser is turned off while a run is in flight.** The run keeps the tools it started with. The next run is offered none.

## Requirements *(mandatory)*

### Functional Requirements

**Availability**

- **FR-001**: The agent MUST be offered the browser tools only when the browser is turned on for the run (FR-017) and the run has a sandbox. Otherwise the run MUST behave as it does today, with no browser tools and no browser.
- **FR-001a**: When the browser is turned on for a run but the sandbox service cannot run browsers, the run MUST be offered no browser tools, as if the browser were off. DAIV MUST log the cause at ERROR, and the site configuration page MUST warn that the sandbox service needs upgrading while the site switch is on.
- **FR-002**: The browser MUST be off by default on fresh and upgraded installs.
- **FR-003**: Whether a run has the browser MUST be decided when the run starts, and MUST NOT change while it runs.
- **FR-004**: A browser MUST start on the agent's first browser call, not when the run starts.

**Isolation and reach**

- **FR-005**: The browser MUST run beside the run's sandbox, inside the sandbox's isolation and network boundary, never in DAIV's own processes. It MUST reach the sandbox's local services. DAIV MUST provide the browser, so it works whatever the environment's base image is.
- **FR-006**: The browser MUST reach external hosts only as the run's sandbox environment's egress policy allows, for every request a page makes, including scripts, images, frames and redirects. With network off, it MUST reach no external host. A refused page navigation MUST tell the agent which host was refused. The browser's egress is the run's effective egress: the environment's policy plus the git-platform access DAIV opens for the run, as for the sandbox's own commands.
- **FR-007**: Each run MUST get a browser of its own. A session's first browser MUST start empty, with no cookies, storage, history or signed-in state. A session's browser profile MUST NOT reach another session.
- **FR-007a**: When a run that used the browser ends, whatever its outcome, DAIV MUST save the session's browser profile. The session's next run MUST start its browser from that profile and re-open the saved addresses, and its first browser call's result MUST list the pages it re-opened. Other page state, such as form contents and scroll positions, is not kept.
- **FR-007b**: The browser profile MUST live and die with the session's sandbox. It MUST be discarded whenever the sandbox is replaced or discarded, and when the session expires or is deleted. A run whose profile cannot be restored MUST start an empty browser, and its first browser call's result MUST say so.
- **FR-008**: DAIV MUST NOT put the person's credentials, DAIV's git credentials or the sandbox environment's secrets into the browser, its tool results or its screenshots' metadata. A saved browser profile holds sign-in cookies, so it MUST be readable only by its session's runs, and MUST never reach a log, a transcript or a tool result.
- **FR-009**: A run's browser MUST be closed when the run ends, whatever the outcome: succeeded, failed, cancelled or timed out. No browser MUST outlive the sandbox session it serves.
- **FR-010**: The browser MUST refuse file downloads.

**Driving the browser**

- **FR-011**: The agent MUST be able to open an address and to click, hover, type, fill fields, select options, press keys and scroll on the page, naming elements by their ids in a page snapshot.
- **FR-012**: The agent MUST be able to read a page snapshot of the current page, including the content of its frames.
- **FR-013**: The agent MUST be able to take a screenshot of the visible page or the whole page, and MUST see it as an image when its model reads images.
- **FR-014**: The browser tools MUST work with the run's own model, and MUST NOT need another model, provider account or API key.
- **FR-015**: The browser tools' descriptions MUST tell the agent that page content is untrusted input, never instructions.
- **FR-016**: Each browser call MUST be bounded in time. A call that passes its limit MUST fail with a result that says so, and the run MUST continue.

**Settings**

- **FR-017**: A run MUST get the browser only when both of these are on:
  - the site switch, a site-wide setting that only administrators can change, in Site Configuration and through its environment variable;
  - the Browser field of the run's sandbox environment, which whoever manages that environment can change, as for Network.
- **FR-017a**: A change to either setting MUST apply from the next run that starts, and MUST NOT replace any session's sandbox.
- **FR-017b**: The browser MUST run under its own memory and CPU limits, which only administrators set, once for the whole site. They default to 1 GiB and 1 CPU. The browser MUST NOT count against the environment's Memory and CPUs, and a browser that passes its memory limit MUST NOT stop the sandbox, the agent's commands or its local services.

**What people see**

- **FR-018**: Every screenshot the agent takes MUST appear in the session transcript with the call that took it, under the existing limits on media in tool results.
- **FR-019**: Each browser call MUST appear in the transcript with a readable label that says what the agent did, such as which address it opened, how many actions it ran, or that it took a screenshot.

**Failures**

- **FR-020**: When the browser cannot start, crashes or disconnects, the browser tool MUST return a result that says so, without internal error text or secrets. DAIV MUST log the cause at ERROR. After a crash or disconnect, the next browser call MUST start a fresh browser from the session's last saved profile. Its result MUST say that the pages and state since that profile was saved were lost.
- **FR-021**: When the run's model cannot read images, a screenshot call MUST still keep the screenshot for the transcript. Its result MUST tell the agent it cannot view the image, and to use the page snapshot instead.

**Documentation**

- **FR-022**: The user documentation MUST describe the browser: what the agent can do with it, what it can reach, how to turn it on, what carries over between a session's runs, its limits and its memory cost. The site configuration reference MUST list the browser switch and its memory and CPU limits, the sandbox environments page MUST describe the Browser field, and the changelog MUST record the feature.

### Key Entities

- **Run browser**: the browser one run uses. It is created on the run's first browser call and closed when the run ends. It belongs to one run and one sandbox session, and starts from the session's browser profile.
- **Browser profile**: a session's saved cookies, site storage and open tab addresses. It is saved when a run that used the browser ends, restored by the session's next run, and discarded with the session's sandbox. It is readable only by that session's runs.
- **Browser settings**: the site switch, which administrators control, and the Browser field of each sandbox environment, which its manager controls. Both are off by default, and a run gets the browser only when both are on (FR-017). The site also sets the browser's memory and CPU limits (FR-017b).
- **Screenshot**: an image a browser call returned. It is kept as the call's tool-result media, shown in the transcript and subject to the run's media limits.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In an eval case where the agent fixes a visible defect in a small web application, the agent opens the running page, and its report includes a screenshot of the fixed state, in a stable majority of votes.
- **SC-002**: In an eval case whose answer appears only after a page's scripts run, on a page served from the sandbox, the agent answers correctly in a stable majority of votes. The same case without the browser fails.
- **SC-003**: Every attempt to reach a host outside the run's egress policy is refused, across navigations, sub-resources and redirects: zero leaks in the test suite.
- **SC-004**: No browser outlives its run. After runs that succeed, fail, are cancelled and time out, no browser from them is still running one minute after the run ended.
- **SC-005**: The agent's first browser call opens a page from a local service, measured from the start of the call, in under 5 seconds at the median when the sandbox runtime is runc, and under 8 seconds when it is gVisor.
- **SC-006**: With the browser on, the project's existing prompt-eval suites show no confirmed regression against a baseline with it off. A run that never uses the browser uses at most 2% more input tokens.
- **SC-007**: With the browser off, runs offer no browser tools and start no browser: zero browser processes across the test suite's off-path runs.
- **SC-008**: A sign-in to a local service in one run of a session is still in effect in the session's next run, in every test. No cookie from one session is ever present in another session's browser.

## Assumptions

- **Local browser only.** Browserbase's hosted browsers are out of scope. They would send the pages the agent opens, and the sandbox's local services, to a third party, and would need a Browserbase account. An operator option for them can come later.
- **The browser's egress is the run's effective egress.** It is the environment's policy plus the git-platform access DAIV opens for the run, as for the sandbox's own commands. A page on an allowed host can therefore reach the git platform with the run's credential, as a package script the agent runs already can.
- **DAIV provides the browser, beside the sandbox.** An environment's base image needs no browser of its own. Running the browser inside the sandbox's boundary, and reaching it from DAIV, needs a matching daiv-sandbox release. That dependency ships with the feature, as the project requires for any change to the sandbox's wire schema. Until the sandbox service is upgraded, runs get no browser (FR-001a).
- **The global default's Browser field is not inherited.** Like Network, each environment sets its own Browser field.
- **The library is the basis.** The integration uses the package the user named (published as `langchain-stagehand`) and its three tools. DAIV adds the isolation, lifecycle, settings, transcript display and failure handling around it. Its version is pinned as the project pins every dependency.
- **One browser per run, one profile per session.** The browser itself lives for one run, as the sandbox stops at the end of every run. The session's browser profile carries cookies, storage and open addresses to the session's next run.
- **The main agent only.** Subagents are not offered the browser in this version, since concurrent agents would each need a browser of their own.
- **Screenshots, not a live view.** People see what the browser saw through screenshots in the transcript. A live view of the browser, or taking it over, is out of scope.
- **No approval step before browser actions.** The browser's reach is bounded by the egress policy and its empty profile, and unattended runs could not wait for an approval.
- **Both git platforms.** Nothing in the feature depends on GitLab or GitHub, and it works the same on both.
- **Memory cost.** A browser uses roughly 300 MB under the sandbox's runtime on a simple page, from the spike, within its own limit of 1 GiB by default. Operators size their hosts for one browser per concurrent run that uses it, and the documentation says so.
- **Measurement.** Adding tools changes what the model sees, so evals follow the project's agent-evals rules: a baseline with the browser off, recorded before the change, and the same model and suites after it.
