# Feature Specification: Merge Request Automations

**Feature Branch**: `005-merge-request-automations`

**Created**: 2026-11-21

**Status**: Draft

**Input**: User description: "" (the command ran without a description; the spec follows the design discussion that preceded it). That discussion concluded:

- Running DAIV on every push today takes a CI job that posts an `@daiv /code-review` comment with a second platform token, or a jobs API call whose result never reaches the merge request and re-reviews the whole branch each time.
- The need is general: "run this prompt on that merge request when something happens". Code review is one prompt among many, so there is no review-specific switch.
- There is no workflow language. CI systems already chain steps, conditions and variables.
- The person who triggered a run owns it for attribution (spend, visibility). The run never gets that person's personal tools, personal sandbox environments or secrets.
- Automation runs send no notifications. Their results and their failures appear on the merge request, and their spend goes to the person who triggered them.
- Automations may run on merge requests DAIV published when the repository opts in, with guards so that they cannot loop.

## Clarifications

### Session 2026-11-21

- Q: Which session does a run that targets a merge request use? → A: A job behaves like a mention by its caller: the owning session when a DAIV session owns the merge request, otherwise the merge request's own session. Each automation uses a session of its own per merge request, which the loop guards on DAIV-published merge requests need (FR-006, FR-024).
- Q: Which refinements from planning does the spec adopt? → A: All three listed in the plan's research, approved with the plan: SR1 (FR-005), SR2 (FR-011) and SR3 (FR-016).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a prompt on a merge request from CI or a script (Priority: P1)

A maintainer wants every push to a merge request reviewed. They add a CI job that asks DAIV to run `/code-review` on the pipeline's merge request. DAIV runs the prompt with that merge request as its context, the same context a mention on it gives, and posts the run's final message as one comment on the merge request. Each later push produces a report that covers only the commits since the previous report.

**Why this priority**: It removes both of today's workarounds, works for any prompt, not only review, and is the base that stories 3 and 4 reuse.

**Independent Test**: Submit a job that names an open merge request with the prompt `/code-review`; one review comment appears on it. Push a commit and submit again; the second report covers only the new commit. Submit a third time with no new commit; DAIV answers that the change was already reviewed.

**Acceptance Scenarios**:

1. **Given** an open merge request in a repository the caller can write to, **When** the caller submits a job that names it with a prompt, **Then** DAIV runs the prompt with the merge request as context and posts the final message as one comment on the merge request from DAIV's account.
2. **Given** a job that names a merge request, **When** the caller reads the job's status, **Then** the status holds the same result as the comment, as for any job.
3. **Given** a merge request that is closed, merged, missing or in another repository, **When** a job names it, **Then** DAIV refuses the job before starting a run and says why.
4. **Given** a job that names a merge request, **When** its run fails, **Then** DAIV posts a short comment on the merge request saying the run could not complete, and the job status is failed.
5. **Given** a caller without write access to the repository, **When** they name one of its merge requests, **Then** DAIV answers as it does today for a repository the caller cannot reach, without confirming the merge request exists.
6. **Given** a merge request no DAIV session owns, **When** a job names it and a person later mentions DAIV there, **Then** both run in the merge request's own session, so the mention has the job's result in context.
7. **Given** a merge request a DAIV session owns, **When** a job names it, **Then** the run is a turn of the owning session acting as the caller, as a mention by the caller would be, and it waits for that session's current turn to end.

---

### User Story 2 - CI signs in as the pipeline, and the run belongs to whoever triggered it (Priority: P2)

A maintainer sets up the CI job without storing a DAIV key. The job presents the short-lived identity token that the git platform signs for each CI job. DAIV checks that the platform signed it, that it was issued for DAIV, and that it belongs to a pipeline of the repository the job targets. DAIV attributes the run to the person who triggered the pipeline.

**Why this priority**: It removes a long-lived secret that any branch's CI configuration can read, and puts each run's spend on the person who caused it instead of on the owner of a shared key. Story 1 works without it, with an API key.

**Independent Test**: Run a pipeline whose job presents its identity token; the run appears under the person who pushed. Present a token from another repository, for another audience, expired, or not signed by the platform; each is refused.

**Acceptance Scenarios**:

1. **Given** a valid identity token from a pipeline of repository R, **When** the job targets R, **Then** DAIV accepts it and attributes the run to the platform account that triggered the pipeline: its DAIV account when one is linked, otherwise the platform account marked external.
2. **Given** a token issued to a pipeline of repository S, **When** the job targets repository R, **Then** DAIV refuses it.
3. **Given** a token issued for another audience, an expired token, or one the configured platform did not sign, **When** it is presented, **Then** DAIV refuses it without saying which check failed.
4. **Given** a job authenticated by an identity token, **When** its run starts, **Then** it loads no personal tools and no personal sandbox environment of the triggering person, even when that person has a linked DAIV account.
5. **Given** a request authenticated by an identity token that also names another user, a second repository or an existing thread, **When** it is submitted, **Then** DAIV refuses it.

---

### User Story 3 - Repository automations that need no CI (Priority: P3)

A maintainer declares in the repository's DAIV configuration: when a merge request is opened, receives new commits, or is marked ready, run this prompt, limited by filters on drafts, target branches and changed paths. DAIV starts the runs itself from the platform's events and posts each result on the merge request.

**Why this priority**: It serves repositories without CI access or with many projects to cover, and it is configuration that is reviewed and versioned like code. Stories 1 and 2 already cover the CI route.

**Independent Test**: Declare an automation for `/code-review` on opened and updated merge requests, and merge it to the default branch. Open a merge request: a report appears. Push: a report covering the new commits appears. Open a draft: nothing happens until it is marked ready.

**Acceptance Scenarios**:

1. **Given** an automation declared on the default branch, **When** a merge request that matches its filters is opened, **Then** DAIV runs its prompt on that merge request and posts the result there.
2. **Given** an automation declared only on a feature branch, **When** that branch's merge request is opened or updated, **Then** the automation does not run.
3. **Given** several pushes to one merge request within a short window, **When** the window closes, **Then** DAIV starts one run, on the newest commit.
4. **Given** a draft merge request and an automation that does not include drafts, **When** the draft is opened or updated, **Then** no run starts; **When** it is marked ready, **Then** the automation runs.
5. **Given** an automation run, **When** it starts, **Then** it is attributed to the platform account whose action caused the event, it loads no personal tools or personal sandbox environment, it sends no notifications, and it cannot ask a question.
6. **Given** an event the platform delivers twice, or a commit this automation already handled on this merge request, **When** it arrives, **Then** no second run starts.
7. **Given** a configuration with one invalid automation entry, **When** DAIV reads it, **Then** it ignores that entry, reports it to the operator, and applies the rest of the configuration.

---

### User Story 4 - Automations on DAIV's own merge requests, without loops (Priority: P4)

A maintainer wants DAIV's own changes reviewed as well. They opt an automation in for merge requests that DAIV published. When DAIV pushes to one of them, from a chat, a job, a mention or a CI fix, the automation runs. A commit that an automation pushes never starts another automation, and automation results never start work in the session that owns the merge request on their own.

**Why this priority**: It extends story 3 to DAIV's own work, which needs loop guards that the other stories do not.

**Independent Test**: Opt an automation in and have a DAIV session push to its merge request; the automation runs once. Configure an automation whose prompt always pushes a commit; one person's push produces exactly one automation run.

**Acceptance Scenarios**:

1. **Given** an automation that has not opted in, **When** DAIV publishes or pushes to its own merge request, **Then** the automation does not run.
2. **Given** an automation that has opted in, **When** a DAIV session pushes to the merge request it owns, **Then** the automation runs in a session separate from the owning one and posts its result on the merge request.
3. **Given** an automation run that pushes a commit, **When** the push arrives, **Then** that commit starts no automation on the merge request.
4. **Given** an automation result on a merge request a DAIV session owns, **When** it is posted, **Then** the owning session starts no turn from it; a person who mentions DAIV on the merge request starts one.
5. **Given** a merge request whose automation runs started by DAIV's own pushes reached the cap, **When** DAIV pushes again, **Then** no automation runs and DAIV posts one comment saying the cap was reached; **When** a person pushes, **Then** the automations run and the cap is restored.

---

### Edge Cases

- A merge request closes or merges while its automation run waits to start: the run does not start. A run that already started completes and posts its result.
- A force-push replaces reviewed commits: the next code review report covers the whole change and says so, as reviews do today.
- The triggering person has no DAIV account, or an inactive one: the run still starts and is attributed to the platform account, marked external.
- The repository restricts who may use DAIV to an allowed list of usernames, and the triggering person is not on it: no run starts, for both identity-token jobs and automations.
- Several automations match one event: each runs on its own, once.
- A CI job and an automation run the same prompt on the same merge request: both run. DAIV does not merge runs across trigger types.
- An automation's prompt needs an answer from a person: the run cannot ask, so it finishes with what it has and says what it could not decide.
- A pipeline runs in a fork: the identity token names the fork, so a job that targets the upstream repository is refused.
- The operator turns automations off site-wide: no new automation run starts; runs already started finish.

## Requirements *(mandatory)*

### Functional Requirements

**Merge request target**

- **FR-001**: A job submission MUST be able to name an open merge request of the targeted repository in place of a branch, through the jobs API and the MCP job tool.
- **FR-002**: A run for a job that names a merge request MUST have the same merge request context that a mention on that merge request gives, and work on its source branch.
- **FR-003**: DAIV MUST post the final message of a run that targets a merge request as one comment on the merge request, from DAIV's account, and still return it in the job status.
- **FR-004**: DAIV MUST refuse, before starting a run, a job that names a merge request that is not open, does not exist, or belongs to another repository, and say which.
- **FR-005**: Naming a merge request MUST NOT widen access: the caller needs the same repository access as for any job, and an unknown or unreachable repository gets the same answer it gets today. For a job authenticated by a CI identity token, the platform-signed token naming the repository is the access proof.
- **FR-006**: A job that names a merge request MUST run where a mention by the caller on that merge request would: in the session that owns it when a DAIV session does, acting as the caller and waiting for that session's next turn when it is busy, otherwise in the merge request's own session. Automations do not follow this rule; each uses a session of its own per automation and merge request (FR-024).

**CI identity**

- **FR-007**: The jobs API MUST accept, in place of an API key, an identity token that the configured git platform signs for a CI job.
- **FR-008**: DAIV MUST accept such a token only when the platform's published signing keys verify it, its issuer is the configured platform, its audience is DAIV's own address, it has not expired, and the repository it names is the job's repository. A refusal MUST NOT say which check failed.
- **FR-009**: A token-authenticated submission MUST target exactly the token's repository, MUST NOT continue an existing thread, and MUST NOT name a user.
- **FR-010**: DAIV MUST attribute a token-authenticated run to the platform account that triggered the pipeline: its linked, active DAIV account, otherwise the platform account marked external. A value in the request body MUST never decide attribution.
- **FR-011**: Runs authenticated by an identity token MUST load global tools only, never the triggering person's personal tools or personal sandbox environments. They MUST send no notifications unless the request asks for them.
- **FR-012**: The repository's allowed-usernames list MUST apply to the triggering account, and the jobs rate limit MUST count token-authenticated submissions per triggering account.
- **FR-013**: CI identity MUST work on GitLab and GitHub.

**Repository automations**

- **FR-014**: The repository's DAIV configuration MUST let a maintainer declare automations, each with a name, the events it runs on, a prompt and filters. DAIV MUST read automations only from the default branch, as it reads the rest of that configuration.
- **FR-015**: Automations MUST support three events with the same meaning on GitLab and GitHub: a merge request opened, a merge request that received new commits, and a draft merge request marked ready.
- **FR-016**: Automations MUST support four filters: include drafts (default off), target branches (patterns, default any), changed paths (patterns, default any) and DAIV-published merge requests (default off). When DAIV cannot list every changed path, the paths filter MUST let the run start.
- **FR-017**: DAIV MUST start at most one run per automation per merge request head commit, and MUST combine events for one merge request that arrive within a short window into one run on the newest commit.
- **FR-018**: DAIV MUST attribute an automation run to the platform account whose action caused the event, under the same rules as FR-010, FR-011 and FR-012.
- **FR-019**: Automation runs MUST send no notifications and MUST NOT be able to ask a question.
- **FR-020**: An automation run MUST post its final message on the merge request as in FR-003. A run that fails or cannot complete MUST post a short comment saying the automation could not complete.
- **FR-021**: DAIV MUST ignore an invalid automation entry on its own, report it to the operator as an error, and apply the rest of the configuration.
- **FR-022**: Operators MUST be able to turn automations off for the whole site.
- **FR-023**: Each automation run MUST show which automation and which event started it, in the session and in spend reports, under a trigger type of its own.

**DAIV-published merge requests**

- **FR-024**: An automation MUST run on a merge request a DAIV session owns only when it opts in, and then in a session separate from the owning one.
- **FR-025**: An automation result MUST NOT start a turn in the session that owns the merge request.
- **FR-026**: A commit pushed by an automation run MUST NOT start any automation.
- **FR-027**: Per merge request, DAIV MUST cap the automation runs started by commits that DAIV pushed (default 3, set by the operator). When the cap is reached, DAIV MUST post one comment saying so. A commit pushed by a person MUST restore the cap, and commits pushed by people MUST never be capped.

**Documentation**

- **FR-028**: The user documentation MUST describe the merge request target, CI identity tokens and automations, with a CI example for GitLab and for GitHub, and MUST replace the comment-posting workaround as the recommended setup.

### Key Entities

- **Merge request target**: a repository and a merge request number, given on a job in place of a branch.
- **CI identity**: the short-lived, platform-signed token of one CI job: the repository, the account that triggered the pipeline, the commit, the audience and the expiry.
- **Automation**: an entry in the repository's DAIV configuration: name, events, prompt, filters, and whether it runs on DAIV-published merge requests.
- **Automation run**: one run started by an automation: the automation's name, the merge request, the head commit, the triggering account and the outcome. It is the record that stops duplicates (FR-017), loops (FR-026) and runs past the cap (FR-027).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A maintainer who follows the documentation sets up a review on every merge request push with one CI job, no DAIV secret and no second platform token, in under 10 minutes.
- **SC-002**: Every run that targets a merge request, whether from a job or an automation, ends with exactly one comment on the merge request: its result or a failure note.
- **SC-003**: Five pushes to one merge request within the combining window start one automation run, and an event delivered twice starts one run.
- **SC-004**: With an automation whose prompt always pushes a commit, one push by a person starts exactly one automation run.
- **SC-005**: Every automation run and every token-authenticated run appears in spend reports under the person who triggered it, or their external platform account, and none under DAIV.
- **SC-006**: No run starts from a forged, expired, wrong-audience or wrong-repository identity token.
- **SC-007**: No automation run or token-authenticated run loads a personal tool or a personal sandbox environment.

## Assumptions

- The combining window is about one minute, long enough to absorb a burst of pushes without delaying a review noticeably.
- Code review's incremental reports, "already reviewed" answers and force-push handling come from the existing `/code-review` skill; this feature gives it the merge request context it needs and changes nothing in it.
- Automation runs use the model that the repository and site configure for the run and for the skill it invokes. There is no per-automation model in this version.
- Automations work whether or not the repository's pull request assistant is on, since the repository declares them explicitly.
- Schedules do not get a merge request target: merge requests are short-lived and schedules are not.
- Out of scope for this version: automation events for comments, issues, pipelines and pushes to branches without a merge request; conditions on a run's result; chaining one automation to another.
- CI identity relies on GitLab CI ID tokens and GitHub Actions OIDC tokens. A self-hosted GitLab issues tokens under the instance address DAIV is already configured with.
- A run for a triggering person without a DAIV account starts and is attributed to their platform account, as webhook runs are today.
- The existing jobs rate limit applies, and the spend dashboard needs no new breakdowns beyond the new trigger types.
