# Feature Specification: Delegated Leg Follow-ups

**Feature Branch**: `claude/delegated-leg-follow-ups`

**Created**: 2026-11-09

**Status**: Draft

**Input**: User description: "Delegated leg follow-ups and full leg replies for the orchestration coordinator." The design discussion that preceded it concluded:

- Deep Agents' async subagents are not a fit for DAIV's subagents. The background-work pattern they offer already exists as delegation, which lacks only a way to continue a leg.
- A continued leg works in its own session and pushes to its own merge request, instead of a new session stacking a second merge request on the first.
- A follow-up reaches the leg's session through its inbox, the only way an existing session is asked to act.
- The coordinator should see a leg's reply in full up to a much higher limit than today's 1,000 characters.
- Success is measured with the `delegation` prompt-eval suite (PR #1780), before and after.

## Terms

- **Coordinator**: the session whose run delegated work.
- **Leg**: one piece of delegated work. It runs in a session of its own whose parent is the coordinator, and publishes to a branch and merge request of its own.
- **Batch**: the legs one delegate call started or continued. The coordinator is resumed once, after everything in the batch has finished.
- **Follow-up**: a request from the coordinator to continue a leg it delegated earlier, in that leg's session.
- **Leg reply**: the leg's final answer, which the coordinator reads when it is resumed or checks on a batch.

## Clarifications

### Session 2026-11-09

- Q: When a leg's merge request is already merged or closed, what should a follow-up to that leg do? → A: It is refused when sent. If the merge request merges or closes while the follow-up waits, the follow-up stops when it starts, without changing anything, and tells the coordinator to delegate a new leg. Recorded in FR-008 and FR-008a.
- Q: When a follow-up arrives at a leg's session that already has other requests waiting on the same branch, should it share that run or run alone? → A: Always alone. Other waiting requests get runs of their own, before or after it, and the follow-up still sees remembered events as context. Recorded in FR-004.
- Q: Should the coordinator be allowed to send a follow-up to a leg DAIV started but lost track of? → A: No. It is refused, saying the leg may still be working and to check its session; the coordinator can delegate a new leg if the work is needed. Recorded in FR-008, and in the Assumptions on which outcomes can be followed up.
- Q: Should follow-ups be checked for the person's repository access when sent, as new legs are, or only when their run starts? → A: Only when the run starts, by the inbox's existing check. A start refused for access ends the follow-up as failed, and the batch's summary reports it. Recorded in FR-005a and the Edge Cases.
- Q: When a leg's session has already hit the inbox's limit of three DAIV-started runs in a row, should a follow-up still wake it? → A: Yes. A follow-up always wakes the leg's session, as a person's request does. Its run still counts as DAIV-started, so CI fixes after it stay limited. Recorded in FR-004a and the Edge Cases.
- Q: Archiving hides a session from one person's list only. Whose archive should block a follow-up? → A: The archive of the person the follow-up runs as, the one who triggered the coordinator's run, as chat refuses today. The refusal says to unarchive the leg's session or delegate a new leg. Recorded in FR-008, FR-009 and the Edge Cases.
- Q: When a follow-up's run fails, should the inbox retry it as it retries other inbox events, or should it end as failed the first time? → A: It is not retried. The first failed run ends the follow-up as failed, as a new leg fails today, and the coordinator decides whether to send another. Recorded in FR-011a and the Edge Cases.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The coordinator continues a leg in the leg's own session and merge request (Priority: P1)

A person asks DAIV for work across several repositories. The coordinator delegates one leg per repository and is resumed when they finish. One leg reports that its change is incomplete: its tests still fail. The coordinator sends that leg a follow-up. The leg picks up where it stopped, with its earlier conversation, and pushes the fix to the merge request it already opened. The coordinator is resumed once the follow-up finishes, and reports one merge request per repository, not two.

**Why this priority**: Today a new batch is the coordinator's only option when a leg comes back incomplete or wrong. That starts a new session with a fresh clone and none of the leg's context, and opens a second merge request on top of the first, because every delegated run forks a new branch. Reviewers then get two merge requests for one change, and the second leg spends its budget rediscovering what the first already knew.

**Independent Test**: A coordinator delegates one leg. The leg publishes a merge request, and its reply says it left a failing test. The coordinator delegates to that leg again by naming it, with a new request. The leg's session shows a second run that continues the first. The commits land on the leg's existing branch, the leg still has exactly one merge request, and the coordinator is resumed exactly once after the follow-up finishes.

**Acceptance Scenarios**:

1. **Given** a finished leg that published a merge request on its own branch, **When** the coordinator sends it a follow-up, **Then** a new run starts in the leg's session with the leg's earlier conversation, its changes are pushed to the leg's branch and merge request, and no new session, branch or merge request is created.
2. **Given** a finished leg that published nothing and started from the default branch, **When** the coordinator sends it a follow-up that makes a change, **Then** the leg opens a branch and merge request of its own, as a new leg would.
3. **Given** a finished leg that published nothing and started from the coordinator's own branch, **When** the coordinator sends it a follow-up that makes a change, **Then** the change goes to a new branch and merge request of the leg's own, never to the coordinator's branch or merge request.
4. **Given** the coordinator sent a follow-up, **When** the follow-up's run finishes, **Then** the coordinator is resumed once, with a summary that covers the follow-up.
5. **Given** one delegate call starts a new leg on one repository and continues an existing leg on another, **When** both have finished, **Then** the coordinator is resumed once, with a summary covering both.
6. **Given** a follow-up runs, **When** it would need input, **Then** it decides or reports instead of stopping to ask, runs as the person who triggered the coordinator's run, and keeps the coordinator's mute.

---

### User Story 2 - The coordinator is told, per leg, why a follow-up cannot be sent (Priority: P1)

The coordinator names a leg it cannot continue. That target is refused with a reason that says what to do instead, while the call's other targets go ahead.

**Why this priority**: A follow-up acts on an existing session, branch and merge request. Sending one to the wrong session, to one already busy with an earlier request, or to a merge request that is already merged would push work where nobody expects it. Refusing clearly keeps the coordinator from silently losing work or acting outside its own legs.

**Independent Test**: In one call, the coordinator names a session that is not one of its legs, a leg still working on an earlier follow-up, and a valid finished leg. The first two come back refused, each with its reason. The third starts, and the batch resumes the coordinator once it has finished.

**Acceptance Scenarios**:

1. **Given** a session that is not a leg this coordinator delegated, **When** the coordinator names it as a follow-up target, **Then** that target is refused as not one of its legs, and nothing is sent to that session.
2. **Given** a leg whose earlier delegated work has not finished, or that still has a follow-up waiting, **When** the coordinator sends it another follow-up, **Then** that target is refused, saying the leg is still working and the coordinator will be resumed when it finishes.
3. **Given** a leg DAIV started but lost track of, **When** the coordinator sends it a follow-up, **Then** that target is refused, saying the leg may still be working and to check its session.
4. **Given** a leg whose merge request has been merged or closed, **When** the coordinator sends it a follow-up, **Then** that target is refused, saying to delegate a new leg for further work.
5. **Given** a follow-up target that also names a branch, **When** the call is made, **Then** that target is refused, saying a follow-up works on the leg's own branch.
6. **Given** a follow-up target whose repository is not the leg's, **When** the call is made, **Then** that target is refused, naming the leg's repository.
7. **Given** one call names the same leg twice, **When** the call is made, **Then** the whole call is refused and nothing starts.
8. **Given** a refused target alongside valid ones, **When** the call is made, **Then** the valid ones start, and the result lists each refusal next to what started.

---

### User Story 3 - A follow-up to a busy leg waits its turn and keeps the batch open (Priority: P2)

A leg's session may be busy when the follow-up arrives. It might be fixing CI on its merge request, or a person might be chatting in it. The follow-up waits until the session is free and then runs, with whatever happened meanwhile (for example a person pushing to the branch) as context. The coordinator is not resumed until the follow-up has run. A follow-up that has not started can be cancelled without disturbing anything else waiting on the leg's session.

**Why this priority**: Leg sessions keep working after the batch that started them: they fix CI on their merge requests and answer mentions there. A follow-up must neither run beside that work, nor be lost behind it, nor let the batch resume early.

**Independent Test**: A leg's session is running a CI-fix run. The coordinator sends it a follow-up. The follow-up starts only after the CI-fix run ends, its input includes events remembered meanwhile, and the coordinator is resumed after the follow-up finishes, not when the CI-fix run does.

**Acceptance Scenarios**:

1. **Given** a leg session running a run, **When** a follow-up arrives, **Then** no second run starts in parallel, and the follow-up runs after the current run ends.
2. **Given** a person pushed to the leg's branch before the follow-up runs, **When** it runs, **Then** it starts from the branch as pushed and sees that push as context.
3. **Given** a follow-up is waiting, **When** the batch's other legs finish, **Then** the coordinator is not resumed until the follow-up has finished too.
4. **Given** a follow-up is waiting and another request (for example a CI failure) is also waiting on the leg's session, **When** the coordinator cancels the follow-up, **Then** only the follow-up is withdrawn and the other request still runs.
5. **Given** a follow-up is running, **When** the coordinator cancels it, **Then** it stops as a running leg stops today.
6. **Given** every target of a batch was a follow-up and all were cancelled before starting, **When** the cancel completes, **Then** no resume comes for that batch, as for a batch whose every leg was cancelled.

---

### User Story 4 - The coordinator reads a leg's whole reply (Priority: P2)

A leg investigates and writes a long report. The coordinator is resumed with that report in full, up to a limit several times today's, and can quote its findings back to the person who asked. When a reply is longer than the limit, the coordinator is told it was cut and where to find the rest.

**Why this priority**: The coordinator decides from leg replies whether a leg is done, needs a follow-up, or calls for new work. Today it sees at most the first 1,000 characters, taken from a copy already cut to 2,000 when stored. A finding at the end of a report never reaches it, and with follow-ups that decision matters more.

**Independent Test**: A leg replies with a report of about 2,600 characters whose decisive finding comes last. The coordinator's resume input and its check on that batch both contain the finding, and the coordinator's report to the person states it.

**Acceptance Scenarios**:

1. **Given** a leg reply of up to about 4,000 characters, **When** the coordinator is resumed, **Then** the summary contains the reply in full.
2. **Given** a leg reply longer than that, **When** the coordinator is resumed, **Then** the summary contains its first part and says it was cut, pointing to the leg's session for the rest.
3. **Given** the coordinator checks one batch, **When** the result comes back, **Then** each leg's reply is shown under the same limit as in the resume.
4. **Given** the coordinator checks its latest batches without naming one, **When** the result comes back, **Then** replies stay at the shorter limit used today, so the overview stays readable.

---

### User Story 5 - The coordinator knows how to address a leg, and the session page shows each leg once (Priority: P3)

The resume summary and the batch status name each leg's session id, so the coordinator can address a follow-up without parsing a link. The coordinator's instructions say when to follow up (fixing or extending a leg's own work) and when to delegate a new leg (separate work). On the coordinator's session page, a leg that served several batches is listed once, with its latest state. The batch details show it under each batch it served, and the delegate call's card marks a follow-up target as one.

**Why this priority**: Without the id, the coordinator cannot target a follow-up reliably. Without clear guidance, it either never follows up or follows up on work that belongs in a new leg. Listing a leg twice inflates the counts a person reads at a glance.

**Independent Test**: After a batch with one follow-up, the resume summary names each leg's session id. The coordinator's page shows "N delegated" counting each leg session once, while the batch details list the follow-up leg under both batches.

**Acceptance Scenarios**:

1. **Given** a finished batch, **When** the coordinator is resumed, **Then** each leg's entry names the leg's session id alongside its session link, branch, merge request and reply.
2. **Given** a leg that served two batches, **When** a person opens the coordinator's session, **Then** the top-level count and list include that leg once, and the batch details show it under each batch.
3. **Given** a delegate call with a follow-up target, **When** the call's card is shown, **Then** the follow-up target is matched to its leg by session and marked as a follow-up.
4. **Given** a resume where a leg reports its own work incomplete, **When** the coordinator acts, **Then** it sends that leg a follow-up rather than delegating a new leg.
5. **Given** a resume where the original request also has an independent second part, **When** the coordinator acts on that part, **Then** it delegates a new leg rather than a follow-up.

### Edge Cases

- **A leg on a repository whose access the person lost.** The follow-up is accepted when sent, because access is checked only when its run starts (FR-005a). The run is then refused, ends as failed, and the batch's summary reports it without DAIV's internal error text. The batch still resumes the coordinator.
- **A leg whose earlier conversation is no longer available.** The follow-up still runs, from its request and the state of the leg's branch.
- **A leg session that was archived.** When the person the follow-up runs as archived it, the follow-up is refused, saying to unarchive the session or delegate a new leg. Another person's archive only hides the session from their own list, and the follow-up goes ahead.
- **A leg session at its limit of DAIV-started runs in a row**, for example after its first run and two CI fixes. The follow-up still wakes it and runs (FR-004a). A CI failure after that follow-up is still only remembered.
- **A follow-up whose run fails partway**, for example on a model or sandbox error. It is not retried (FR-011a). It ends as failed, and the batch resumes the coordinator once its other runs have ended.
- **A coordinator that keeps sending follow-ups that fail.** It is bounded by the existing limit of three runs in a row nobody asked for on the coordinator's session. After that, a finished batch is only remembered, and its owner is told.
- **A follow-up sent to a leg that is itself waiting to start after the coordinator's turn ends.** It is refused as still working.
- **A person's own requests on the leg's session waiting at the same time.** The follow-up runs in a run of its own, never combined with them, so it stays unattended and answers only the coordinator.
- **The leg's merge request merged or closed while the follow-up was waiting.** The follow-up stops when it starts, without changes, and the batch's summary tells the coordinator to delegate a new leg (FR-008a).
- **More than the per-call target limit.** Follow-ups count toward the same limit as new legs, and the call is refused as today.

## Requirements *(mandatory)*

### Functional Requirements

**Sending a follow-up**

- **FR-001**: A delegate call MUST accept, per target, the session id of a leg the coordinator delegated earlier. That target continues the leg instead of starting a new one.
- **FR-002**: A follow-up MUST run in the leg's existing session, with the leg's earlier conversation available to it.
- **FR-003**: A follow-up MUST push its changes to the leg's own branch and merge request when the leg has published one. When the leg has not, it MUST open a branch and merge request of its own, and MUST NOT push to the branch or merge request the leg started from.
- **FR-004**: A follow-up MUST reach the leg's session as an inbox event. It waits while the session is busy and receives the session's remembered events as context. It always runs alone: it never shares a run with another waiting request, whether DAIV's own (such as a CI failure) or a person's. Those requests get runs of their own, before or after it, in the order the inbox serves them.
- **FR-004a**: A follow-up MUST wake the leg's session even when the session has reached the limit of three DAIV-started runs in a row, as a person's request does. Its run MUST still count as DAIV-started in that session's streak, so CI failures after it stay subject to the limit.
- **FR-005**: A follow-up MUST run unattended: it never stops to ask a question. It runs as the person who triggered the coordinator's run, with the coordinator's mute.
- **FR-005a**: A follow-up's repository access MUST be checked when its run starts, by the check every inbox run gets. It is not checked when the coordinator sends it. A start refused for access MUST end the follow-up as failed, so the batch still finishes and its summary reports that leg as failed, without DAIV's internal error text.
- **FR-006**: One delegate call MAY mix new legs and follow-ups. All of them belong to the one batch the call creates, and they count toward the same per-call target limit.
- **FR-007**: The call's result MUST report each follow-up that was sent in the same form as a started leg: its repository, branch, session id and link, and whether it starts after the coordinator's turn.

**Refusing a follow-up**

- **FR-008**: A follow-up target MUST be refused, without affecting the call's other targets, when:
  - the session is not a leg this coordinator delegated;
  - the person the follow-up runs as has archived the session (another person's archive does not count);
  - the leg's earlier delegated work has not finished;
  - DAIV started the leg's work but lost track of it, so it may still be running;
  - the leg already has a follow-up that has not finished;
  - the leg's merge request is merged or closed;
  - the target names a branch;
  - the target's repository is not the leg's.
- **FR-008a**: A follow-up whose leg's merge request merged or closed after it was sent MUST stop when it starts, without changing the leg's branch or opening a merge request. It finishes in a way the batch's summary reports as not done, and it MUST say to delegate a new leg for the work.
- **FR-009**: Each refusal MUST say what to do instead (wait for the resume, check the leg's session, unarchive the leg's session, delegate a new leg, omit the branch, use the leg's repository), and MUST NOT include DAIV's internal error text.
- **FR-010**: A call that names the same leg more than once MUST be refused as a whole, and nothing in it starts.

**Batch, resume and cancel**

- **FR-011**: A batch MUST count a follow-up from the moment it is sent until its run ends. The coordinator MUST NOT be resumed while a follow-up in the batch is waiting or running.
- **FR-011a**: A follow-up MUST NOT be retried. It gets one run, and that run's end, whether success or failure, ends the follow-up, unlike other inbox events, which are retried up to three times. A failed follow-up is reported in the batch's summary, and the coordinator may send another.
- **FR-012**: The coordinator MUST be resumed exactly once per batch, after every leg and follow-up in it has finished, under the same rules as today: none when nothing started or everything was cancelled, and the limit on runs in a row nobody asked for.
- **FR-013**: Cancelling a follow-up that has not started MUST withdraw only that follow-up, leaving any other request waiting on the leg's session in place. Cancelling a running follow-up MUST stop it as a running leg stops today.
- **FR-014**: Batch status MUST list a follow-up as a leg of the batch that sent it. It MUST report the leg's current branch whenever the leg has a branch of its own, including when the follow-up started on that branch.

**What the coordinator reads**

- **FR-015**: The resume summary and batch status MUST name each leg's session id.
- **FR-016**: The resume summary, and the status of a single named batch, MUST show each leg's reply from its full final answer, up to about 4,000 characters, and MUST mark a reply cut at that limit with a pointer to the leg's session.
- **FR-017**: The status overview across the latest batches MUST keep today's shorter reply limit of 1,000 characters.
- **FR-018**: The coordinator's instructions and the resume summary MUST say to follow up on a leg to fix or extend that leg's own work, and to delegate a new leg for separate work.

**What people see**

- **FR-019**: The coordinator's session page MUST list and count each leg session once in its top-level delegations, with its latest state. Its batch details MUST show a leg under every batch it served.
- **FR-020**: A delegate call's card MUST match each follow-up target to its leg by session id, and mark it as a follow-up.
- **FR-021**: The orchestration documentation and the changelog MUST describe follow-ups, their limits and refusals, and the longer leg replies.

### Key Entities

- **Leg**: a session whose parent is the coordinator, holding one or more delegated runs. Each run belongs to one batch. Its branch and merge request are those it published, if any.
- **Follow-up**: a request from the coordinator to a leg. It has a prompt, its batch, the person it acts as and the coordinator's mute. It becomes one delegated run in the leg's session.
- **Batch**: the new legs and follow-ups one delegate call sent. It is finished when every run in it has ended, and nothing it sent is still waiting.
- **Leg reply**: a leg run's final answer, read in full from the run's result, and shown to the coordinator up to a limit.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the `delegation` eval suite, a resume where a leg reports its own work incomplete is answered with a follow-up to that leg, not a new leg, in a stable majority of votes.
- **SC-002**: In the same suite, a resume whose request has an independent second part is answered with a new leg, not a follow-up, in a stable majority of votes.
- **SC-003**: The suite's five baseline cases show no confirmed regression against the baseline recorded on `main`. The case whose finding sits past today's reply limit changes from failing to passing.
- **SC-004**: Every follow-up to a leg with an open merge request lands on that merge request, and none opens a second merge request for that leg.
- **SC-005**: Every batch that contains follow-ups resumes its coordinator exactly once, after its last run ends.
- **SC-006**: Cancelling a waiting follow-up leaves every other waiting request on the leg's session in place.
- **SC-007**: A leg reply of up to about 4,000 characters reaches the coordinator whole in every resume.

## Assumptions

- **One level deep stays.** A leg, including a follow-up's run, still cannot delegate.
- **No new tools.** Follow-ups are a target option of the existing delegate action, and status and cancel cover them.
- **A finished leg can be followed up whatever its outcome:** succeeded, failed or cancelled. A failed leg's follow-up is how the coordinator retries it with context. The exception is a leg DAIV lost track of, which may still be running and is refused (FR-008).
- **No mid-run steering.** A follow-up never interrupts a running leg, and only one follow-up per leg may be outstanding.
- **Notifications** for a follow-up's run behave as they do for a leg's run today.
- **The leg's transcript** shows the follow-up's request as it shows a delegated leg's first request.
- **The stored summary** of each run (used by notifications and the chat stream) keeps its current length. The longer reply is read from the run's full final answer.
- **The roughly 4,000-character limit per leg** bounds a resume of 10 legs to about 40,000 characters, which the coordinator's context absorbs. The plan may refine the exact value.
- **Measurement** follows the project's agent-evals rules. The baseline is recorded with the suite from PR #1780 on `main` before the feature, and the after-run uses the same model and suites.
