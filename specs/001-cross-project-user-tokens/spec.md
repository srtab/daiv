# Feature Specification: Cross-Project Git Platform Access Under the Requesting User's Identity

**Feature Branch**: `001-cross-project-user-tokens`

**Created**: 2026-09-03

**Status**: Draft

**Input**: User description: "Current implementation don't allow cross projects calls by the agent using gitlab and github tools. To turn this possible, the used tokens to communicate with each clients should be the authenticated user, to make sure it only access and sees what the user has access."

## Overview

Today the agent's GitLab and GitHub tools can only reach the single project the run is attached to. Anything living in a neighbouring project — the upstream issue that explains a bug, the shared library whose merge request breaks this build, the sibling service whose pipeline the change depends on — is invisible to the agent, so the person asking must copy that context in by hand or accept a worse answer.

This feature lets the agent reach other projects, and makes that safe by binding those calls to the identity of the person the run acts for. When the agent looks beyond the attached project, it sees exactly what that person can see, and nothing more. Access decisions stay with the git platform, which already knows who may read which project; DAIV does not build a second, divergent permission model.

The attached project is unaffected. It continues to be reached with the deployment's existing service identity, so the comments, merge requests, and pipelines DAIV already produces keep their current authorship and behaviour. The new identity applies to the new capability only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read context from another project the requester can access (Priority: P1)

A developer asks the agent to fix a failing build in service A. The failure comes from a breaking change in shared library B, which lives in a different project the developer works on daily. The agent looks up the recent merge requests and the issue thread in project B, understands the change, and fixes service A accordingly — without the developer pasting anything.

**Why this priority**: This is the entire point of the feature. Without it, nothing else has value. It is also the smallest slice that delivers a working, demonstrable capability on its own.

**Independent Test**: With a requester who has access to both projects, ask the agent a question that can only be answered from the second project's issues or merge requests, and confirm the answer contains information the agent could not have obtained from the attached project alone.

**Acceptance Scenarios**:

1. **Given** a run acting for a person who can read project B on the git platform, **When** the agent queries an issue in project B, **Then** the agent receives the issue content and can use it in its answer.
2. **Given** the same run, **When** the agent queries the project the run is attached to without naming a project, **Then** the call targets the attached project under the service identity, exactly as before this feature.
3. **Given** a run on GitLab and an equivalent run on GitHub, **When** each agent queries a second project, **Then** both succeed with equivalent capability.

---

### User Story 2 - A person never sees more than they are entitled to (Priority: P1)

A developer who cannot read a private project asks the agent about it, directly or indirectly. The agent is refused by the git platform, receives a clear "you do not have access" result, tells the developer plainly, and continues with what it can do. No content from that project reaches the conversation, the transcript, the produced code, or any published comment.

**Why this priority**: The permission guarantee is what makes cross-project access acceptable at all. It ships with User Story 1 rather than after it, because a capability that leaks is worse than no capability. It is independently testable and independently valuable as an auditable security property.

**Independent Test**: Start a run for a person with no access to a target project, ask the agent to read from it, and confirm the run produces a stated access failure and zero content from that project anywhere in its output.

**Acceptance Scenarios**:

1. **Given** a person with no access to project C, **When** the agent attempts to read project C, **Then** the attempt fails with a message naming the access problem, and no project C content appears in the run.
2. **Given** a person whose access to project C is revoked between two calls in the same run, **When** the agent calls project C again, **Then** the second call fails; a result cached from the first call is not reused to serve content the person may no longer read.
3. **Given** a refused cross-project call, **When** the agent continues, **Then** it does not retry that call under the service identity or any other broader identity.
4. **Given** any cross-project attempt, allowed or refused, **When** an operator reviews the run's record afterwards, **Then** they can see which identity was used and which project was targeted.

---

### User Story 3 - Authorise DAIV during sign-in (Priority: P2)

Signing in to DAIV with GitLab or GitHub asks for enough authorisation to reach other projects on that person's behalf, and the consent screen says what DAIV will be able to reach. Someone who signed in before this change keeps working as they do today and is asked to re-authorise the first time cross-project access is needed. Account settings show what is connected, when it expires, and how to disconnect.

**Why this priority**: Required for real users to get the capability, but User Stories 1 and 2 can be demonstrated end-to-end for a single authorised user before this surface exists. Nobody is blocked from using DAIV while it is missing — only from reaching other projects.

**Independent Test**: Sign in as a new user, complete the consent step, and run a cross-project query successfully; then disconnect the authorisation and confirm the same query fails with a message pointing back to sign-in, while attached-project work still succeeds.

**Acceptance Scenarios**:

1. **Given** a person signing in for the first time, **When** they complete the git platform consent step, **Then** DAIV holds an authorisation sufficient to reach other projects for them, and account settings shows its status.
2. **Given** a person who signed in before this feature and has not re-authorised, **When** the agent attempts a cross-project call, **Then** the call is refused with a message naming the missing authorisation and offering the re-authorisation step, and attached-project work proceeds unaffected.
3. **Given** an authorisation that has expired, **When** the agent needs it, **Then** the system renews it without involving the person where the platform permits, and otherwise asks them to re-authorise.
4. **Given** a person who revokes the authorisation, from DAIV or from the git platform, **When** a later run acts for them, **Then** it cannot use the revoked authorisation and nothing fetched under it is served to another identity.

---

### User Story 4 - Runs started by a platform event act for the person who triggered them (Priority: P2)

Many runs start from a platform event rather than from someone sitting in the DAIV interface — a label added to an issue, a comment on a merge request. For cross-project lookups, such a run acts for the platform user who performed that action, when that person has a DAIV account and a valid authorisation. When they do not, the run stays inside the attached project and says why, rather than reaching further under a broader identity.

**Why this priority**: A large share of real runs arrive this way, and this rule is what stops the security guarantee from being conditional. The primary flow can be built and tested before this path is wired up.

**Independent Test**: Add a bot label to an issue as a platform user who has a linked DAIV account and authorisation, and confirm the resulting run can read a second project that person can access; repeat as a platform user with no linked account, and confirm the run completes within the attached project with the limitation stated.

**Acceptance Scenarios**:

1. **Given** an event triggered by a platform user with a linked DAIV account and a valid authorisation, **When** the agent makes a cross-project call, **Then** that call acts for that person and is bounded by their access.
2. **Given** an event triggered by a platform user with no linked DAIV account, or with a missing or expired authorisation, **When** the agent attempts a cross-project call, **Then** the call is refused and the run's output states that cross-project access was unavailable and why.
3. **Given** either case, **When** the agent works within the attached project, **Then** it behaves exactly as it does today, including the authorship of anything it publishes.

---

### Edge Cases

- A person's authorisation expires part-way through a long run, or a session is resumed days later: cross-project calls must not continue under a stale or substituted identity, and the person must learn that this is why the agent stopped seeing the other project.
- A person can read a project but cannot write to it, and the agent attempts to comment there: the refusal is reported as a permission outcome, not as a malfunction.
- The agent publishes a comment in *another* project under a person's identity, and that project is itself watched by DAIV: the comment is attributed to a person rather than to the bot, so DAIV must not mistake it for a fresh request and start a run. (The attached project is unaffected — it keeps the service identity and today's self-recognition.)
- The target project lives on a different platform or a different host than the attached project: the system either serves it under an authorisation valid for that host or refuses clearly; it never silently targets the wrong host.
- The agent names a project that does not exist, or spells it ambiguously: the result distinguishes "not found" from "not permitted to you" only as far as the git platform itself does, so DAIV does not become an existence oracle for private projects.
- Two people run against the same attached project at once: results fetched under one identity are never served to the other.
- A person's platform account is rate-limited by their own usage elsewhere: the run reports the throttling rather than appearing to find nothing.
- A person's DAIV account is deactivated, or their authorisation revoked, while a scheduled or queued run of theirs is pending.
- A person triggers a platform event, then loses access to a target project before the run reaches it.
- Every existing signed-in user lacks the broader authorisation on the day this ships: their first cross-project attempt must fail informatively, and everything else must keep working.
- The agent asks for a very large amount of cross-project data: existing limits on result size and pagination continue to apply unchanged.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The agent's GitLab and GitHub tools MUST be able to target a project other than the one the run is attached to, when the run's acting person is permitted to reach it.
- **FR-002**: Every call to a project other than the attached one MUST be performed under the identity of the person the run acts for, so the git platform's own permission checks apply to that person.
- **FR-003**: Calls to the attached project MUST continue to use the deployment's existing service identity, leaving today's behaviour and authorship unchanged.
- **FR-004**: When no project is named, a call MUST target the attached project.
- **FR-005**: The system MUST NOT return, quote, summarise, or otherwise expose content from a project the acting person cannot access on the git platform.
- **FR-006**: When a cross-project call is refused for lack of access or authorisation, the system MUST report the refusal to the agent and to the person reading the run's output, naming the project and the reason, and MUST NOT retry that call under the service identity or any other broader identity.
- **FR-007**: DAIV MUST request, during sign-in with GitLab or GitHub, authorisation broad enough to reach other projects on the person's behalf, and the consent step MUST convey what that authorisation permits.
- **FR-008**: Users MUST be able to see the status of that authorisation — connected, expiring, expired, revoked — and disconnect it, from their account settings.
- **FR-009**: The system MUST renew an expired authorisation without involving the person where the platform permits it, and MUST otherwise refuse the cross-project call and ask them to re-authorise.
- **FR-010**: A person who signed in before this feature, and therefore holds only the narrower prior authorisation, MUST retain today's full attached-project capability, and MUST be told what is missing and how to restore it when a cross-project call is attempted.
- **FR-011**: The system MUST tell a person, at the point of failure, when their authorisation is absent, expired, revoked, or insufficient, and how to restore it.
- **FR-012**: Credentials MUST NOT be written to logs, agent-visible output, produced code or diffs, published comments, or run transcripts and checkpoints.
- **FR-013**: Results fetched under one identity MUST NOT be served to another identity, including across concurrent runs on the same attached project.
- **FR-014**: A cross-project call MUST act for the platform user who triggered the run, when the run started from a platform event and that user maps to a DAIV account holding a valid authorisation. When no such mapping exists, cross-project calls MUST be refused and the limitation stated in the run's output; the attached project is unaffected.
- **FR-015**: Content the agent publishes in another project while acting for a person MUST NOT cause DAIV to start a new run, even though that content is attributed to a person rather than to the service identity.
- **FR-016**: The system MUST record, for each cross-project access, which identity was used and which project was targeted, so an operator can audit it afterwards.
- **FR-017**: GitLab and GitHub MUST reach equivalent capability; a gap that cannot be closed MUST be documented in the operator-facing documentation in the same change that ships it.
- **FR-018**: Existing limits on which operations the agent may perform, and on how much output a single call may return, MUST apply to cross-project calls unchanged.
- **FR-019**: Operators MUST be able to run DAIV without this capability enabled, and existing single-project, service-identity behaviour MUST be unchanged when it is off.

### Key Entities *(include if feature involves data)*

- **Acting person**: the human a run acts for when reaching beyond the attached project — the signed-in requester for interface-started runs, or the platform user who triggered the event for event-started runs. May be unresolvable, which removes cross-project access for that run.
- **Platform authorisation**: the per-person grant DAIV holds to act on a git platform as that person, obtained at sign-in. Has an owner, a platform, a host, a validity window, a renewal capability, and a state (connected, expired, revoked). Never shown to the agent or to other users.
- **Service identity**: the existing deployment-level identity that reaches the attached project and authors what DAIV publishes there. Unchanged by this feature.
- **Target project**: the project a given call is addressed to — the attached project by default, another project when named.
- **Access record**: the auditable trace of a cross-project call: who, which project, when, and whether it was permitted.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A person can get an answer that draws on a second project they have access to, in a single request, without pasting any content from that project.
- **SC-002**: In access-control testing, 100% of attempts to reach a project the acting person cannot access are refused, with zero content from those projects appearing in run output.
- **SC-003**: A new user can sign in, consent, and complete a successful cross-project request in under 3 minutes, without operator assistance.
- **SC-004**: Every failure caused by a missing, expired, revoked, or insufficient authorisation produces a message that names the cause and the next step; a reader can act on it without consulting logs or an operator.
- **SC-005**: Runs that stay within the attached project behave identically to before, with no change in success rate and no change in who authored the resulting comments and merge requests.
- **SC-006**: Across a full regression of the comment and label flows, zero runs are started by content DAIV published itself.
- **SC-007**: An operator can determine, for any completed run, which other projects were reached and under whose identity.
- **SC-008**: GitLab and GitHub pass the same cross-project acceptance scenarios.

## Assumptions

- The git platform (GitLab or GitHub) remains the single source of truth for who may access what. DAIV mirrors those decisions and does not maintain its own project permission model.
- The agent's existing policy on which operations are permitted, and its existing output-size and pagination limits, carry over to cross-project calls unchanged. This feature widens *which* projects are reachable, not *what* may be done to them.
- Whether a cross-project call may write (comment, open a merge request) or only read is governed by the acting person's own permissions on the target project, not by an additional DAIV-side read-only restriction.
- Cross-project access is an operator-controlled capability that can be left off, consistent with existing deployments that do not need it.
- Cloning, committing, and pushing code continue to use the existing repository-scoped mechanism. This feature concerns the platform tools the agent looks things up with, not how code is fetched or published.
- Two identities coexist within a single run: the service identity for the attached project, and the acting person for anything beyond it. Audit records therefore distinguish identity per call, not per run.
- Sign-in already links a DAIV account to a git platform account; what is missing is authorisation broad enough to act. The mapping from a platform user back to a DAIV account, needed by FR-014, uses that same link.
- The authorisation obtained at sign-in is as narrow as the platform's consent model allows while still permitting the agent's existing read operations. It is inherently wider than a single repository, because reaching other projects is the point of the feature.
- Existing runs, sessions, and signed-in users continue to work unchanged; they gain cross-project access only after re-authorising.
