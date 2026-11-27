# Feature Specification: Submodules and Git LFS

**Feature Branch**: `claude/magical-hypatia-atqoxm`

**Created**: 2026-11-26

**Status**: Draft (scope widened 2026-11-27 to publish changes inside submodules)

**Input**: User description: "Support git submodules and Git LFS in the repositories DAIV works on" ([#1875](https://github.com/srtab/daiv/issues/1875)). A team evaluating DAIV on GitLab could not use it on their issues: their repositories use submodules heavily and keep some files in LFS. DAIV's checkout leaves submodule directories empty and LFS-tracked files holding only their pointer text. The reporter patched their instance to clone submodules recursively and fetch LFS content, and asked how a change that spans a submodule and its superproject would be published, since that needs a merge request in each repository. The description also recorded what the current code means for the feature:

- The clone credential is minted for one repository: a GitLab project access token, or a GitHub installation token scoped to the repository. A plain recursive clone would send it to every submodule on the same host, the platform would reject it there, and clones of repositories with submodules, which work today, would start failing.
- The worker's clone, `.git` included, is the copy a sandbox run starts from. LFS content would travel twice: once in the checked-out files and once in git's LFS store.
- Verified by experiment: when a sandbox without git-lfs receives a clone whose LFS content was downloaded, git there reports every LFS-tracked file as modified, and DAIV's publish (stage everything, then commit) would commit the files' content in place of their LFS pointers. The default sandbox image has no git-lfs, and neither do DAIV's own images.
- DAIV's publish stages changes in the superproject only. Edits inside a submodule's checkout are either left behind without a word, or make the commit fail when they are the only change.
- Publishing an LFS-tracked file needs its content uploaded to LFS storage before the push, or the platform rejects the push.
- Sessions can already open extra repositories beside the main one and publish a merge request to each.

The reporter's follow-up on 2026-11-27: their issues need changes inside the submodules, all their repositories are on one GitLab instance, and they run their patched instance in the meantime. They asked whether `.daiv.yml` could list projects to open as a multi-repository session automatically.

## Clarifications

### Session 2026-11-26

- Q: Is this feature read-only, or does it also publish changed LFS-tracked files, or changes inside submodules as merge requests in their repositories? → A: Read-only. The agent sees submodule code and LFS content; changes to LFS-tracked files and inside submodules are never published, and the run's reply names them. A change across repositories uses the extra-repositories route (FR-012, FR-013, Assumptions). *Superseded for submodules on 2026-11-27, below; LFS stays read-only.*
- Q: Whose platform access decides that a run may fetch a submodule, including for runs a webhook started? → A: The run's person: DAIV fetches a submodule only when the person the run belongs to can read its repository on the platform. A run whose person DAIV cannot identify on the platform fetches none (FR-005).
- Q: Are submodule checkout and LFS content on by default, or off until a repository turns them on? → A: Off until a repository turns each one on in its DAIV configuration (FR-001).

### Session 2026-11-27

- Q: Does the spec adopt the refinement from planning? → A: Yes, SR1, approved with the plan: a person's access to a submodule's repository is what DAIV's repository access records, which list members only, so a public or internal repository counts only for its members (FR-005).
- Q: May LFS content come from a host other than the platform's? → A: Only where the platform's own LFS API sends DAIV, such as the platform's object storage; never where the repository's configuration points (FR-006, from the `/speckit-analyze` review).
- Q: Should publishing changes inside submodules wait for a later feature, with a read-only first version? → A: No. This feature publishes them too, as merge requests in the submodules' repositories with the superproject pointing at them: the reporter needs those changes and runs a stopgap until then. Changes to LFS-tracked files stay unpublished (US2, FR-019 to FR-026).
- Q: Can someone see a session whose agent read a submodule they can't read? → A: No. The session's visibility and repository memory treat a fetched submodule's repository like an extra repository's (FR-027), found in review: a teammate who can read the superproject only would otherwise read the submodule's code in the transcript.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The agent works with the code in a repository's submodules (Priority: P1)

A maintainer's repository includes libraries as submodules hosted on the same git platform. They turn on submodule checkout in the repository's DAIV configuration and label an issue for DAIV. The agent finds every submodule checked out, nested ones included, at the commit the superproject records for the branch the run works on. It reads and searches that code, and on a sandbox run builds and tests the project with it. DAIV publishes the agent's changes to the superproject as it does today. Changes inside submodules follow User Story 2; a submodule DAIV may not publish keeps the agent's edits out of the publish, and the run's reply names them.

**Why this priority**: Without the submodules' code the agent cannot understand, build or test these repositories at all, which is the issue's blocker. Publishing safely is part of the story: a checkout that lets edits vanish or rewrites the recorded submodule commit would make things worse than today.

**Independent Test**: Turn submodule checkout on for a repository with two submodules, one of them nested, all on the same platform. Run DAIV on an issue that asks a question only the submodules' code can answer; the answer cites that code. Run it on an issue whose fix is in the superproject and whose tests need the submodule; the merge request holds the fix and changes no recorded submodule commit.

**Acceptance Scenarios**:

1. **Given** a repository with submodule checkout on whose submodules the run may fetch, **When** a run starts, disk or sandbox, **Then** every submodule, recursively, is checked out at the commit the superproject records for the run's ref.
2. **Given** that checkout, **When** the agent inspects git status before changing anything, **Then** no submodule shows as changed.
3. **Given** a run that changed files in the superproject and inside a submodule DAIV may not publish (FR-019), **When** DAIV publishes, **Then** the superproject's changes are published, the submodule's recorded commit is unchanged, and the run's reply names the submodule edits that were not published and why.
4. **Given** a run whose only changes are inside a submodule DAIV may not publish, **When** DAIV publishes, **Then** it publishes nothing, the run does not fail on an empty commit, and the reply says the changes were not published and why.
5. **Given** a merge request branch whose remote moved a submodule's recorded commit while the run worked, **When** DAIV integrates the remote and publishes, **Then** the published branch keeps the remote's submodule commit.
6. **Given** a repository that has not turned submodule checkout on, **When** a run starts, **Then** its submodule directories are empty, as today.

---

### User Story 2 - The agent's changes inside a submodule become a merge request in its repository (Priority: P2)

An issue needs a fix in a library the repository includes as a submodule, and a change in the repository that uses it. With submodule checkout on, the agent edits both. DAIV opens a merge request in the submodule's repository with the library change. The superproject's merge request holds its own change and records the submodule at the commit DAIV pushed. The two merge requests link each other, and the superproject's says the submodule's must merge first. If the submodule's merge request is squashed or rebased when it merges, the commit the superproject records disappears from that repository's branches. DAIV then points the superproject at the commit the merge produced, so a merged superproject never records a commit nobody can fetch.

**Why this priority**: The reporter's issues need changes inside submodules; without this the agent can only read them. It builds on User Story 1's checkout, so it comes second.

**Independent Test**: Turn submodule checkout on for a repository with a submodule the person can write. Run DAIV on an issue whose fix spans both. Two merge requests open: one in the submodule's repository with the library change, one in the superproject with its change, recording the submodule at the head of the first one's branch. Squash-merge the submodule's merge request; the superproject's merge request then records the squashed commit.

**Acceptance Scenarios**:

1. **Given** a submodule whose repository the run's person can write, **When** the agent changed files inside it and DAIV publishes, **Then** DAIV opens a merge request in the submodule's repository with those changes, and the superproject's publish records the submodule at the commit DAIV pushed.
2. **Given** a turn whose only changes are inside such a submodule, **When** DAIV publishes, **Then** both merge requests open: the submodule's with the change, the superproject's with the new submodule commit alone.
3. **Given** those merge requests, **When** a reviewer opens them, **Then** each links the other, and the superproject's says the submodule's must merge first.
4. **Given** a nested submodule whose repository and whose parent submodule's repository the person can write, **When** the agent changed the nested one, **Then** DAIV publishes the nested submodule first, then its parent recording it, then the superproject recording the parent: one merge request per repository.
5. **Given** a submodule whose publish fails, **When** DAIV publishes, **Then** the superproject publishes its other changes, keeps the submodule's recorded commit, and the reply names the submodule and the failure.
6. **Given** a later turn in the same session, **When** the agent changes the submodule again, **Then** DAIV updates the same submodule merge request, and the superproject's merge request records its new head.
7. **Given** a submodule merge request DAIV opened is squashed or rebased when it merges, and the superproject's merge request is open and still records the commit DAIV pushed, **When** DAIV learns of the merge, **Then** it updates the superproject's merge request to record the commit the merge produced on the submodule's target branch, and the session says so.
8. **Given** the superproject's merge request merged before the submodule's, and the submodule's is then squashed or rebased, **When** DAIV learns of the merge, **Then** it opens a new superproject merge request that records the commit the merge produced.
9. **Given** a submodule merge request merged with a merge commit, so the commit DAIV pushed is on the target branch, **When** DAIV learns of the merge, **Then** it changes nothing.

---

### User Story 3 - The agent reads the real content of LFS-tracked files (Priority: P3)

A maintainer's repository keeps fixtures, models or assets in LFS. They turn on LFS content in the repository's DAIV configuration. The agent finds those files with their real content, in the superproject and in any submodule DAIV checked out, and can open them, and on a sandbox run use them in builds and tests. Files the agent did not change never appear in a merge request, and an LFS pointer is never replaced by file content, whatever the sandbox image holds.

**Why this priority**: It unblocks the repositories whose code or tests read LFS files. Submodules matter more to the reporter, and LFS content is useless on its own when the code that reads it sits in a submodule.

**Independent Test**: Turn LFS content on for a repository with LFS-tracked files and run DAIV on an issue that needs one of them read; the answer reflects the file's real content. Run it on a sandbox whose image has no git-lfs with an issue fixed outside the LFS files; the merge request contains the fix and no LFS-tracked file.

**Acceptance Scenarios**:

1. **Given** a repository with LFS content on, **When** a run starts, disk or sandbox, **Then** every LFS-tracked file the run may fetch holds its real content.
2. **Given** that checkout on a sandbox whose image has no git-lfs, **When** the agent inspects git status before changing anything, **Then** no LFS-tracked file shows as changed.
3. **Given** a run that changed or created a file LFS tracks, and changed other files, **When** DAIV publishes, **Then** the other files are published, the LFS-tracked path is unchanged on the branch, and the reply names it as not published and why. This holds inside a submodule DAIV publishes too.
4. **Given** a repository that has not turned LFS content on, **When** a run starts, **Then** its LFS-tracked files hold their pointer text, as today.

---

### User Story 4 - What cannot be fetched or published is reported, and the run goes on (Priority: P4)

A repository has a submodule on another host, a submodule the run may not fetch, a submodule the person can read but not write, and more LFS content than the operator allows a clone to download. The run starts anyway, with what could be fetched. The agent knows from the start which submodules and LFS files are missing, which submodules it may change, and why, so it can work around them or tell the person. The operator learns about the failures they can fix.

**Why this priority**: Real repositories mix reachable and unreachable submodules. Failing the whole run over one of them would make turning the feature on riskier than leaving it off.

**Independent Test**: Turn both settings on for a repository with one writable submodule, one read-only submodule, one on another host and one the run may not fetch, and LFS content above the cap. The run starts with the reachable submodules and the LFS files under the cap. The agent's opening context names the writable submodule, the read-only one, the two missing ones and the LFS files left as pointers, each with its reason.

**Acceptance Scenarios**:

1. **Given** a submodule on a host other than the run's git platform, **When** a run starts, **Then** the submodule's directory stays empty, nothing is requested from that host, and the agent is told the submodule was not fetched because it is outside the platform.
2. **Given** a submodule the run may not fetch (see FR-005), **When** a run starts, **Then** its directory stays empty and the agent is told it was not fetched for lack of access.
3. **Given** LFS content beyond the operator's cap, **When** a run starts, **Then** the files past the cap keep their pointer text and the agent is told which ones and why.
4. **Given** a fetch that fails because of DAIV's own platform access or configuration, **When** a run starts, **Then** the operator's logs record a failure they can act on, without any credential.
5. **Given** a checked-out submodule DAIV may not publish (FR-019), **When** a run starts, **Then** the agent is told its changes there will not be published, and why.

### Edge Cases

- A submodule URL is relative (`../lib.git`), HTTPS or SSH on the run's platform host: each resolves to the platform repository it names and is fetched over the same transport as the main clone.
- A submodule URL uses a protocol other than HTTPS or SSH (a local path, `file://`, a helper protocol): it is not fetched, whatever its host.
- A repository's LFS configuration points LFS storage somewhere other than the repository's own platform: that location is not contacted, and the files stay as pointers.
- A submodule records a commit its repository no longer has: that submodule stays empty and is reported; the others are checked out.
- The same repository is a submodule at two paths, or a submodule is also the session's main or one of its extra repositories: each checkout is fetched with that repository's own credential and is unaffected by the other. Only one checkout per repository is published (FR-024).
- A submodule directory or an LFS-tracked file is deleted, renamed or replaced by the agent, or the agent adds a new submodule: treated as a change DAIV does not publish, and named in the reply.
- A sandbox session is reused warm on a later turn: what was fetched for its first turn is still there, and the agent is told again what is missing.
- `.gitmodules` changes on the run's branch, or the agent edits it: the checkout follows the file at the run's ref, and the agent's edits to it are published like any other file.
- A clone token expires between the main clone and the submodule fetches: the fetch mints anew, as the main clone does.
- A run whose person DAIV cannot identify (FR-005): every submodule stays empty and is reported as not fetched for lack of access.
- The superproject's remote branch moved a submodule's recorded commit while the agent changed that submodule: the submodule's merge request is still published, the superproject keeps the remote's commit (FR-014), and the reply says its pointer was not updated and why.
- A person pushes to the submodule's merge request branch between turns: the next turn checks out that branch's head, and the superproject's next publish records it.
- A submodule merge request is closed without merging: DAIV changes nothing; the superproject's merge request still names it.
- The person loses write access to a submodule's repository between turns: from that turn the submodule is read-only, and its open merge request stays as it is.
- A submodule's merge request merges while the superproject's branch no longer records the commit DAIV pushed: DAIV changes nothing and says so in the session.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A repository's DAIV configuration MUST let its maintainers turn submodule checkout and LFS content on separately. Both are off until the repository turns them on. A run reads the settings as it reads the repository's other settings.
- **FR-002**: With submodule checkout on, every clone a run makes of that repository MUST check out its submodules recursively at the commits the superproject records for the run's ref. This covers the main repository and extra repositories, each by its own settings, on disk runs and sandbox runs.
- **FR-003**: DAIV MUST fetch only submodules hosted on the run's own git platform, over HTTPS or SSH URLs on its host, or relative URLs that resolve there. It MUST NOT contact any other host or use any other protocol for a submodule.
- **FR-004**: Each submodule MUST be fetched, and published, with a credential minted for that submodule's repository alone, no broader and no longer-lived than the main repository's. The main repository's credential MUST NOT be sent to any other repository.
- **FR-005**: DAIV MUST fetch a submodule only when the person the run belongs to may read the submodule's repository as DAIV's repository access sees it. For a run a DAIV user started (chat, job, schedule), the rules that decide which repositories that user sees in DAIV apply. For any other run, the access DAIV records for the platform user whose label, mention or pipeline started it applies. DAIV records members only, so a public or internal repository counts as readable only for its members. A run whose person DAIV cannot identify fetches no submodule.
- **FR-006**: With LFS content on, the LFS-tracked files of the repository and of the submodules DAIV checked out MUST hold their real content. DAIV MUST fetch it only through the LFS API of the repository's own git platform, from the storage that API directs it to, and MUST NOT follow repository configuration that points LFS storage elsewhere.
- **FR-007**: The operator MUST be able to cap the LFS content one clone downloads. Files beyond the cap keep their pointer text.
- **FR-008**: A submodule or LFS file that cannot be fetched MUST NOT fail the clone or the run. Its directory stays empty, or the file keeps its pointer, as today.
- **FR-009**: At the start of a run, the agent MUST be told which submodules and LFS files were not fetched, and why: outside the platform, no access, missing at the recorded commit, over the cap, or a fetch failure. It MUST also be told which checked-out submodules DAIV publishes and which it does not, and why (FR-019, FR-024). The text MUST carry no credential, internal configuration or raw error.
- **FR-010**: A fetch that fails because of DAIV's own platform access or configuration MUST be logged so the operator can act on it. A submodule skipped under FR-003 or FR-005 MUST NOT be logged as an error.
- **FR-011**: Right after the checkout, git status in the run's workspace MUST show no change in any submodule or LFS-tracked file, whatever tools the sandbox image holds.
- **FR-012**: With submodule checkout on, a publish MUST NOT change the commit a submodule is recorded at, or include changes made inside a submodule's checkout, except as FR-019 to FR-021 allow. Adding or removing a submodule is not published. With LFS content on, a publish MUST NOT include any path LFS tracks, whether the agent changed, created, renamed or deleted it or left it untouched, in the superproject or in a submodule DAIV publishes. Publishing LFS-tracked files is outside this feature.
- **FR-013**: When the agent changed files that FR-012 keeps out of the publish, DAIV MUST publish the rest of its changes, and the run's reply MUST name what was not published and why. A publish whose only changes are those files publishes nothing and does not fail.
- **FR-014**: When DAIV integrates a remote branch that moved a submodule's recorded commit, the published branch MUST keep the remote's commit.
- **FR-015**: A sandbox run MUST start from a copy that holds the submodules' checkouts and the LFS content, with the LFS content carried once.
- **FR-016**: A repository with both settings off MUST clone, seed its sandbox and publish exactly as today.
- **FR-017**: The feature MUST behave the same on GitLab and GitHub.
- **FR-018**: The user documentation MUST describe the two settings, which submodules and LFS files DAIV fetches and with whose access, the cap, which submodule changes DAIV publishes and how, the merge order and what DAIV does after a submodule's merge request merges, and what is not published and why. The images DAIV ships MUST hold what the worker needs to fetch LFS content, and the deployment docs MUST say so for operators who build their own.
- **FR-019**: With submodule checkout on, DAIV MUST publish the changes inside a checked-out submodule when the run's person may write the submodule's repository as DAIV's repository access sees it, and every submodule between it and the repository that contains it is published under the same rule. Changes inside any other submodule are kept out (FR-012, FR-013).
- **FR-020**: DAIV MUST publish a submodule's changes on a working branch of the submodule's repository that starts at the commit its parent records, and open a merge request targeting the branch `.gitmodules` names for that submodule when the repository has it, and the repository's default branch otherwise.
- **FR-021**: The parent of a submodule DAIV published MUST record the submodule at exactly the commit DAIV pushed, in the same publish, and only after that push succeeded. DAIV publishes the innermost submodules first. A submodule whose publish fails leaves its parent's recorded commit as it was, and the reply names the failure.
- **FR-022**: Each submodule merge request MUST link the merge request of the repository that records it, and that merge request MUST link the submodule's and say it merges first.
- **FR-023**: Later turns of a session MUST continue each published submodule on its working branch, updating the same merge request, as extra repositories do. When that branch is gone, the next change opens a new merge request.
- **FR-024**: A session MUST publish at most one checkout per repository. When a submodule's repository is also the session's main or an extra repository, that checkout is the one published. When the same repository is a submodule at two paths, the first path in checkout order is. The agent is told which checkouts are not published, and why.
- **FR-025**: When a submodule merge request DAIV opened merges and the commit DAIV pushed is not on the submodule's target branch, DAIV MUST make its parent record the commit the merge produced. While the parent's merge request is open, DAIV updates it; when it has merged, DAIV opens a new one. When the parent's branch no longer records the commit DAIV pushed, DAIV changes nothing. In each case the session says what DAIV did.
- **FR-026**: The session page and the job status MUST list each submodule merge request and each submodule publish failure, as they do for extra repositories.
- **FR-027**: A session in which DAIV fetched a submodule MUST be visible only to people who may read that submodule's repository, as for an extra repository, and MUST NOT add observations to the repository's memory.

### Key Entities *(include if feature involves data)*

- **Submodule checkout**: one submodule of a cloned repository: its path, the URL in `.gitmodules` at the run's ref, the commit the superproject records, the platform repository the URL resolves to, whether it was fetched or why not, and whether DAIV publishes changes inside it or why not.
- **LFS file**: an LFS-tracked path in a cloned repository: its pointer, whether its content was fetched or why not.
- **Checkout report**: what one clone fetched and left out, with reasons. The agent reads it at the start of each run, and the publish uses it to tell the files it keeps out and the submodules it publishes.
- **Session repository**: a repository a session works in beyond its main one. Today, an extra repository the person chose. With this feature, also each submodule repository DAIV fetched, with its path and parent; a published one carries its working branch and merge request like an extra repository.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On repositories whose submodules are all on the platform and fetchable, 100% of runs with submodule checkout on see every submodule's files at the recorded commit, on both platforms and in both run modes.
- **SC-002**: Across the feature's test matrix, no published change touches a path LFS tracks, and no published change includes an edit inside a submodule, or a new recorded submodule commit, other than those FR-019 to FR-021 allow.
- **SC-003**: No run fails because a submodule or LFS file could not be fetched, and in 100% of such runs the agent's opening context names each missing item with its reason.
- **SC-004**: No change the agent makes is lost silently: each one is either published or named in the run's reply as not published.
- **SC-005**: Repositories with both settings off see no change in clone time beyond normal variation, or in the size of the copy a sandbox starts from.
- **SC-006**: For a repository with LFS content on, the copy a sandbox starts from is no larger than its checkout plus its git history, with the LFS content counted once.
- **SC-007**: Across the feature's test matrix, every submodule commit a published branch records exists in the submodule's repository when the branch is pushed, and after DAIV handles a squash or rebase merge of a submodule merge request, the parent's open or new merge request records a commit on the submodule's target branch.
- **SC-008**: No person who cannot read a fetched submodule's repository can open a session in which DAIV fetched it.

## Assumptions

- Publishing changed LFS-tracked files is a later feature.
- Submodules on other hosts, public ones included, are out of this feature. An operator allow-list of hosts could add them later.
- Nested submodules follow their superproject's setting; they have no settings of their own.
- The settings are read from the repository's default branch, like its other DAIV settings, so a merge request cannot turn them on for its own runs.
- Turning submodule checkout on also lets DAIV publish changes inside submodules the person can write; no separate setting turns publishing on. A repository that wants read-only submodules leaves its people without write access to them.
- DAIV publishes to a submodule's repository with the same kind of credential, and needs the same platform access there, as for any repository it publishes to. A push the platform refuses is a publish failure (FR-021).
- Reviewers merge a submodule's merge request before the superproject's, as the superproject's merge request says. DAIV does not block the superproject's merge itself; FR-025 repairs the pointer either way.
- DAIV's image gains git-lfs. A sandbox image needs no git-lfs for anything in this feature.
- The default LFS cap is chosen in planning, sized so that ordinary fixture and asset sets fit and a repository of large binaries does not exhaust the worker's disk.
- Telling the agent about missing submodules and LFS files, about which submodules it may change, and about changes not published, changes what the model sees, so it is measured as the agent evals rules require.
- Listing companion repositories in `.daiv.yml` to open a multi-repository session automatically, which the reporter also asked about, is a separate feature. For repositories that are submodules, this feature covers it.
