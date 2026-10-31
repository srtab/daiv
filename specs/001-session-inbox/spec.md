# Feature Specification: Session Inbox

**Feature Branch**: `001-session-inbox`

**Created**: 2026-10-30

**Status**: Draft

**Input**: User description: "use the session to context what needs to be built". The design discussion that preceded it concluded:

- CI failures on a merge request DAIV published should be handled in the session that published it, not in a separate per-merge-request conversation.
- Everything that asks an existing session to act should arrive through one inbox per session.
- Events are combined into one turn, and an event either wakes the agent or is only remembered.
- The agent does not report a structured verdict; the system decides from facts it can observe.

## Clarifications

### Session 2026-10-30

- Q: Are mentions of DAIV on a merge request DAIV published routed to the owning session, and whose identity does that turn act under? → A: Routed to the owning session, and the turn acts as the owner. The comment is content, and its author is recorded for attribution.
- Q: When a different session publishes to a merge request that another session already owns, who should own it afterwards? → A: Ownership moves only to a later session of the same person. When a different person's session publishes, the original session keeps ownership and records that push as a remembered event.
- Q: When the session that owns a merge request has no DAIV user as its owner, what should CI failures and mentions do? → A: Turns run as today's runs with no DAIV user do: no personal tools, no per-user access check, and the merge request comment as the only notification.
- Q: When a delegated job's session publishes a merge request and CI later fails on it, should the session that delegated the job hear about it? → A: CI is handled in the delegated job's session, and each outcome (passing, stopped after N attempts, a CI turn that pushed nothing) also reaches the delegating session as a remembered event.
- Q: When a waking event arrives at a session that isn't running a turn, how soon should its turn start? → A: Within 1 minute.
- Q: Which refinements from planning does the spec adopt? → A: All seven listed in the plan's research (accepted with the plan, 2026-10-30). They are recorded in FR-010, FR-018, FR-021, FR-028, FR-030 and the Assumptions.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - CI failures are handled in the session that produced the merge request (Priority: P1)

A person starts work with DAIV: an issue, a chat, or a job. The agent publishes a merge request, and CI fails on it. The person sees the failure, the agent's response to it, and the resulting CI status in the session they started. The agent responds knowing why it made the change.

**Why this priority**: Today the response runs in a separate conversation that has no owner and none of the original context. The originating session shows "Attempts exhausted" with nothing in it that explains why. On 2026-10-30 this happened twice in one repository (merge requests 8 and 9):
- The agent correctly found that a flaky, non-code CI step was to blame, and pushed nothing.
- The watch gave up after 1 of 3 attempts.
- Nobody was notified.
- A manual retry later passed, but no session showed it.

**Independent Test**: An issue-triggered session publishes a merge request, and CI fails on it. The follow-up turn appears in that same session, labelled as started by CI. The session's CI status matches the merge request's real pipeline status throughout, including after a manual retry.

**Acceptance Scenarios**:

1. **Given** a session that published a merge request with the CI watch enabled, **When** the merge request's head pipeline fails on a commit DAIV pushed, **Then** a turn starts in that session describing the failed jobs and the pipeline, and no other conversation is created for it.
2. **Given** that turn finds the failure is not caused by the code and pushes nothing, **When** the owner opens the session, **Then** it shows CI as failing together with the agent's explanation (not "attempts exhausted"), and the owner has been notified with that explanation.
3. **Given** the situation in scenario 2, **When** a person retries the pipeline and it passes, **Then** the session shows CI as passing, and no agent turn starts.
4. **Given** the agent has already examined a failure on a commit, **When** another pipeline fails on that same commit, **Then** the failure is recorded and no new turn starts.
5. **Given** the agent pushed a fix and the new commit's pipeline fails, **When** attempt budget remains, **Then** another turn starts. **When** the budget is spent, **Then** no turn starts, the owner is notified, and the session shows "stopped after N attempts" with the failing jobs.
6. **Given** session A published a merge request and session B, owned by the same person, later publishes to it, **When** CI fails after B's push, **Then** the failure goes to session B.
7. **Given** session A published a merge request and a session owned by a different person later publishes to it, **When** that push lands, **Then** session A keeps ownership and records the push as a remembered event naming that session and person. A CI failure after the push goes to session A.

---

### User Story 2 - Events that arrive while a session is busy are handled together in its next turn (Priority: P2)

While a session is running a turn, new events for it wait. When the turn ends, one new turn handles everything that arrived. An event that a newer one replaced appears only as replaced. Nothing is lost, a session never runs two turns at once, and waiting events take no processing capacity away from other work.

**Why this priority**: Once CI results go to the session that published the merge request, that session will often be busy, for example with its owner chatting. Without combining, every event costs a separate turn and can act on information that is already stale. Without waiting that costs nothing, a busy session ties up capacity that other jobs need.

**Independent Test**: While a session runs a long turn, send it a CI failure, a mention, and a newer passing pipeline for the same merge request. Exactly one follow-up turn starts when the first one ends. Its input lists the mention and shows the failure as replaced by the passing pipeline.

**Acceptance Scenarios**:

1. **Given** a session is running a turn, **When** a CI failure arrives for it, **Then** no second turn starts in parallel, and one new turn starts after the current one ends.
2. **Given** a CI failure, a mention, and a newer passing pipeline arrive while a turn is running, **When** that turn ends, **Then** exactly one new turn starts and its input contains the mention and the replaced failure.
3. **Given** the git platform delivers the same event twice, **When** both deliveries arrive, **Then** the event is recorded once and starts at most one turn.
4. **Given** a turn took pending events and then ended without finishing (for example, a crash), **When** the system recovers, **Then** those events become pending again and are delivered in a later turn, up to a retry limit. After the limit, they are marked failed and the operator is alerted.
5. **Given** a turn started by the system is running, **When** the owner sends a chat message, **Then** the message is accepted, the owner can see the system turn in progress and stop it, and their message is handled in the next turn.

---

### User Story 3 - Things the agent should know but need not act on are remembered (Priority: P3)

Some events don't justify an agent turn but matter the next time the agent works, for example:
- CI passed;
- a person pushed commits to the branch;
- the merge request was merged or closed;
- a CI failure that does not wake the agent.

These events are recorded and shown in the session's timeline. The next turn receives them as context, whatever starts that turn.

**Why this priority**: These events make the session's status truthful without spending agent turns, and stop the agent working from an outdated picture, for example overwriting a branch a person moved.

**Independent Test**: Let a pipeline pass and push a commit by hand to the merge request's branch. No turn starts and both events appear in the timeline. Then send a chat message: the turn's input mentions both, and the agent works from the new head of the branch.

**Acceptance Scenarios**:

1. **Given** a session owns a merge request, **When** its pipeline passes, **Then** no turn starts, and the timeline and CI status show the pass.
2. **Given** a person pushes commits to the merge request's branch, **When** the next turn starts for any reason, **Then** the agent is told the branch moved, and it works from the current head.
3. **Given** the merge request is merged or closed, **When** the owner next talks to the session, **Then** the agent's input says so.
4. **Given** many remembered events have built up about the same subject, **When** a turn starts, **Then** it receives only the latest one per subject, and older ones it replaced are dropped.

---

### User Story 4 - One delivery path for everything that asks an existing session to act (Priority: P4)

Three things ask an existing session to act today:
- CI results;
- the results of jobs the agent delegated, which resume the session that delegated them;
- follow-up jobs submitted to an existing session.

All of them reach the session through the same inbox. Waiting, combining, de-duplication, access checks and failure reporting therefore behave the same whatever the source. Operators have one place to see what is waiting or failed.

**Why this priority**: Each source currently has its own delivery path, and the paths behave differently. For example, a CI fix run waiting for a busy session holds processing capacity, while a resumed delegation does not. Having one path stops each new source adding another.

**Independent Test**: While the session that delegated a batch is busy, let the batch finish and also submit a follow-up job to that session. Both arrive through the inbox and are handled together in one turn after the current one. Both are visible to an operator while they wait.

**Acceptance Scenarios**:

1. **Given** a delegated batch finishes while the session that delegated it is busy, **When** that session's turn ends, **Then** the batch result is delivered in the next turn, combined with any other pending events.
2. **Given** a follow-up job targets a busy session, **When** it is submitted, **Then** it waits in the inbox. It is neither rejected nor run in parallel.
3. **Given** the user a turn would act for has lost access to the repository, **When** that turn would start, **Then** it does not run, and the events it would have handled are marked failed with the reason shown in the session.
4. **Given** a session has pending or failed events, **When** an operator inspects the session, **Then** each event's source, arrival time and state are visible.
5. **Given** a delegated job's session owns a merge request, **When** CI on it ends passing, stopped after N attempts, or with a CI turn that pushed nothing, **Then** the delegated session handles it, and the delegating session records the outcome as a remembered event without starting a turn.

---

### User Story 5 - Mentions on a DAIV-published merge request reach the session that owns it (Priority: P5)

When someone mentions DAIV on a merge request a session owns, the mention goes to that session, whether it is in a comment on the merge request or in a review comment on its diff. The agent handles it with the context of the original work and acts as the session's owner. Its reply appears on the merge request, where the person asked.

**Why this priority**: It gives the merge request one coherent history and lets review feedback benefit from the original context. It comes after CI because a mention can now make the agent act with the owner's access on someone else's request. That is accepted on purpose, and FR-026 and FR-027 bound it.

**Independent Test**: A session publishes a merge request. A reviewer mentions DAIV in a comment asking for a change. The turn appears in the owning session, attributed to the reviewer and acting as the owner. The reply is posted on the merge request, and no other conversation is created.

**Acceptance Scenarios**:

1. **Given** a session owns a merge request, **When** a reviewer mentions DAIV in a comment on it, **Then** a turn starts in the owning session, acting as the owner. Its input names the reviewer and quotes the comment.
2. **Given** that turn replies, **When** it finishes, **Then** the reply is posted on the merge request, in the thread where DAIV was mentioned.
3. **Given** the owning session is busy, **When** a mention arrives together with a CI failure, **Then** one later turn handles both, and the reply to the mention is still posted on the merge request.
4. **Given** someone DAIV would not accept a mention from today, **When** they mention DAIV on an owned merge request, **Then** no turn starts, and the mention is handled as it is today for such a person.
5. **Given** a comment on an owned merge request does not mention DAIV, **When** the next turn starts for any reason, **Then** the comment is part of its input as context.

---

### Edge Cases

- **The owning session no longer exists** (deleted): later events go to a new session for that merge request, owned by the same person, and that session's timeline says why it exists.
- **The owning session has no DAIV user** (for example, an issue labelled by someone with no DAIV account): CI failures and mentions still start turns. Those turns have no personal tools and skip the per-user access check, as such runs do today. Every notification falls back to a comment on the merge request.
- **A merge request DAIV did not publish**: it has no owning session and no CI watch, and mentions on it behave as they do today.
- **A mention on an owned merge request whose owner is muted, or whose session has hit the consecutive-turn limit**: a mention comes from a person, so it still starts a turn, and it resets that limit.
- **A failure on a commit a person pushed**: it is remembered, not acted on, so the agent does not take over a person's in-progress work.
- **Events delivered out of order** (an older pipeline reported after a newer one): the older one is treated as replaced and wakes nothing.
- **The CI watch is turned off** for the repository, site, or merge request, **or the owner muted system turns**: CI events are still remembered, and none wakes the agent.
- **A session the system keeps waking** with no person involved: after a fixed number of consecutive system-started turns without human input, further events are remembered only and the owner is notified.
- **The session is waiting for the owner to answer a question** the agent asked: a waking event still starts a turn, and the open question stays visible to the owner.
- **Combined input is too large**: the oldest remembered events are summarised or dropped first. Events that wake the agent are never dropped.
- **Delivery keeps failing** (the platform or a service is unavailable): the event is retried a bounded number of times, then marked failed and reported to the operator as an error.

## Requirements *(mandatory)*

### Functional Requirements

**Ownership and routing**

- **FR-001**: When a session's run publishes to a merge request, by opening it or pushing to it, the system MUST make that session the merge request's owning session if the merge request has no owner, or if its current owning session belongs to the same person. When a session owned by a different person publishes, ownership MUST NOT change, and the push MUST be delivered to the owning session as a remembered event naming the publishing session and its owner. Exception: an owning session with no DAIV user MUST hand ownership to a publishing session that has one.
- **FR-002**: The system MUST deliver every CI result on an owned merge request to the owning session's inbox, and MUST NOT create a separate conversation to handle it.
- **FR-003**: A session's page MUST show the current CI status of every merge request the session owns, worked out from the events delivered to it.
- **FR-004**: If an owning session no longer exists, the system MUST deliver to a new session for that merge request, owned by the same person.

**Inbox and delivery**

- **FR-005**: Every event MUST carry an identity that is unique to its source. A duplicate delivery MUST be recorded at most once.
- **FR-006**: When an event arrives, the system MUST classify it either as waking (it warrants an agent turn) or as remembered (context only).
- **FR-007**: A session MUST run at most one turn at a time.
- **FR-008**: A newer event about the same subject MUST replace an older one. An example is a later pipeline on the same merge request.
- **FR-009**: A turn MUST start only when, after replaced events have been collapsed, at least one pending event is a waking event.
- **FR-010**: When a turn starts, it MUST take every pending event, waking and remembered, that concerns the branch the turn works on, or no branch in particular, and give them to the agent as one combined input in order of arrival, with replaced events marked as replaced or left out. A turn works on one branch; events about another branch wait for a turn on that branch.
- **FR-011**: Events that arrive during a turn MUST wait for the next turn.
- **FR-012**: Pending events MUST NOT hold processing capacity while they wait.
- **FR-013**: If a turn ends without finishing, the events it took MUST return to pending. After a bounded number of retries, they MUST be marked failed, and the failure MUST be reported to the operator as an error.
- **FR-014**: The combined input MUST stay within a size bound. When over it, the oldest remembered events are summarised or dropped first, and waking events are never dropped.

**CI watch**

- **FR-015**: A CI failure MUST wake the session only when all of these hold:
  - the CI watch is enabled for the repository and for the merge request, and the owner has not muted system turns;
  - the failed pipeline belongs to the merge request's head commit;
  - DAIV pushed that commit;
  - the agent has not already examined a failure on that commit;
  - the merge request's attempt budget is not spent.

  Otherwise the failure MUST be remembered only.
- **FR-016**: The attempt budget MUST be counted per merge request, charged only for turns that a CI failure woke, and reset when a run requested by a person publishes to the merge request.
- **FR-017**: When the budget is spent and CI fails on a new commit DAIV pushed, the system MUST notify the owner and show "stopped after N attempts" with the failing jobs.
- **FR-018**: When a turn that a CI failure woke pushes nothing, the system MUST NOT show the watch as given up. It MUST show the agent's final explanation and notify the owner with it. Every turn a CI failure woke MUST notify the owner of its outcome, whether or not it pushed.
- **FR-019**: The system MUST keep recording CI results after a give-up or a turn that pushed nothing, so that a later pass or failure is reflected in the session's status.
- **FR-020**: The system MUST decide whether to wake from facts it can observe: the pipeline, the commit, who pushed it, and the budget. It MUST NOT require the agent to report a structured verdict.

**Other sources**

- **FR-021**: Results of delegated jobs, and follow-up jobs submitted to an existing session, MUST be delivered through the same inbox and follow FR-005 to FR-014. CI on a merge request a delegated job's session owns MUST be handled in that session. Each of its CI outcomes (passing, stopped after N attempts, a CI turn that pushed nothing) MUST also be delivered to the delegating session as a remembered event. A follow-up job submitted while the session is busy joins the session's next turn on the job's branch, and reports that turn as its job, which other events or jobs on that branch may share.
- **FR-022**: Before a turn runs for a user, the system MUST confirm that user still has access to the repository. If not, the turn MUST NOT run, and its events MUST be marked failed with the reason shown in the session. A session with no DAIV user skips this check, and its turns run as today's runs with no DAIV user do, with no personal tools.
- **FR-023**: These events MUST at least be remembered:
  - CI passed;
  - a CI failure that does not wake the agent, with the reason;
  - commits pushed to the merge request's branch by someone other than DAIV;
  - pushes to an owned merge request by a DAIV session that belongs to a different person;
  - CI outcomes of merge requests owned by sessions this session delegated;
  - the merge request merged or closed;
  - comments on an owned merge request that do not mention DAIV.
- **FR-024**: When the branch has moved since the agent's last turn, the next turn MUST tell the agent so, and the agent MUST work from the current head.

**Mentions on owned merge requests**

- **FR-025**: A mention of DAIV on a merge request a session owns MUST be delivered to the owning session's inbox as a waking event, whether it is in a comment or in a review comment on the diff. The system MUST NOT create a separate conversation for it.
- **FR-026**: A mention MUST start a turn only if its author passes the same check that a mention needs today. Otherwise it is handled as it is today for that author.
- **FR-027**: The turn that handles a mention MUST act as the session's owner, and its input MUST name the mention's author and quote the comment.
- **FR-028**: The reply to a mention MUST be posted on the merge request, in the thread where DAIV was mentioned, including when the turn also handled other events. When one turn handles several mentions on a merge request, the full reply goes to the latest mention's thread, and each other mention's thread gets a link to it.

**Visibility and control**

- **FR-029**: The session timeline MUST show turns started by the system separately from turns started by a person, and name their source (CI pipeline, delegated jobs, follow-up job, or a mention and its author).
- **FR-030**: The owner MUST be able to stop a running system-started turn from the session page. The events a stopped turn took are dismissed, not retried.
- **FR-031**: The owner MUST be able to mute system-started turns for a session. While muted, CI and delegation events are remembered only, and mentions from people still start turns.
- **FR-032**: A chat message the owner sends while a system-started turn is running MUST be accepted and handled in the next turn, never rejected.
- **FR-033**: After a fixed number of consecutive system-started turns with no human input in between (default 3), the system MUST stop waking the session, remember further events only, and notify the owner. A chat message or a mention counts as human input.
- **FR-034**: Operators MUST be able to see each session's pending and failed events, with their source, arrival time and state.
- **FR-035**: The feature MUST behave the same on GitLab and GitHub.

### Key Entities

- **Session**: one piece of agent work and its conversation. It has an owner (the person it acts for), the turns it has run, its inbox, and the merge requests it owns.
- **Merge request ownership**: links a merge request on a repository to its owning session. That is the most recent session that published to it, among sessions owned by the person who first published it. Ownership never passes to another person. It holds the merge request's attempt budget and the last commit the agent examined for CI.
- **Inbox event**: something that happened which concerns a session. It has a source identity (unique per source), a kind, the subject it is about (used for replacement), a waking or remembered classification, its author (who caused it, for attribution; turns always act as the owner), its arrival time, and a state (pending, taken by a turn, handled, replaced or failed).
- **Turn**: one agent execution in a session. It records which events it took, whether a person or the system started it and from which source, and its outcome.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of agent turns that respond to CI failures on DAIV-published merge requests appear in the session that published the merge request. No separate conversation is created for CI handling.
- **SC-002**: A CI failure the agent finds is not caused by the code never leads the session to show "attempts exhausted". Replaying the 2026-10-30 merge request 9 scenario ends with the session showing CI as passing after the manual retry.
- **SC-003**: No session ever runs two turns at once. Any number of events arriving during one turn produce at most one follow-up turn.
- **SC-004**: Events waiting on busy sessions delay no other job: with every waiting event's session busy, unrelated jobs start as soon as they would with no events pending.
- **SC-005**: Duplicate deliveries from the git platform produce no duplicate turns and no duplicate merge request comments.
- **SC-006**: A repeated CI failure on a commit the agent has already examined starts no additional turn.
- **SC-007**: Every CI give-up, and every CI-started turn that pushed nothing, produces an owner notification that contains the agent's explanation.
- **SC-008**: An owner can find the explanation for every CI-related outcome from the session they started, without opening another conversation.
- **SC-009**: When a waking event reaches a session that is not running a turn, the turn handling it starts within 1 minute.

## Assumptions

- The existing CI watch switches stay: the site-wide on/off, the per-repository opt-out, and the attempt cap with its default of 3. So does the ability to turn the watch off on a merge request.
- Ownership and the CI watch apply only to merge requests DAIV published. Merge requests a person created keep today's behaviour.
- Watches already open when this ships finish under the old behaviour. Existing per-merge-request conversations are not migrated.
- The agent's final message is the explanation shown and sent. A short label for status displays may come from the post-run classification that already exists, not from the agent.
- Chat turns may keep running interactively for responsiveness. Only their scheduling relative to other turns follows the inbox rules.
- The limit of consecutive system-started turns reuses the existing limit on how many times delegations may resume a session in a row (3).
- Notifications use the channels and preferences that already exist.
- Turns run with the owner's identity, as they do today for CI fix runs and resumed delegations. This includes turns started by another person's mention: the owner's access and personal tools are then used on that person's request. This was accepted in the 2026-10-30 clarification, and is limited by the check on who may mention DAIV (FR-026).
- Today a mention on a DAIV-published merge request runs as its author in the merge request's own conversation. After this feature, that holds only for merge requests no session owns.
- On GitHub, mentions in diff review comments are seen only once the operator subscribes the GitHub App to review-comment events. Until then, they are ignored, as they are today.
- Out of scope, and unchanged:
  - work started by issue webhooks on an issue's own session keeps its current delivery;
  - GitHub CI that does not run on GitHub Actions remains unseen by the CI watch.
