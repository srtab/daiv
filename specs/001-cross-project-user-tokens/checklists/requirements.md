# Specification Quality Checklist: Cross-Project Git Platform Access Under the Requesting User's Identity

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-03
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

**Iteration 1 (2026-09-03)** — All items passed except the clarification marker; three markers
raised, on the rule for runs with no signed-in requester, the identity used for the attached
project, and how the credential is obtained.

**Iteration 2 (2026-09-03)** — Clarifications resolved and folded in.

**Iteration 3 (2026-09-03)** — The attached-project identity answer was changed to **A**
(requester identity for other projects only; attached project keeps the service identity). The
spec was rewritten accordingly. All items pass.

Resolved decisions now encoded in the spec:

- **Runs with no signed-in requester** (FR-014). Event-started runs act for the platform user who
  added the label or wrote the comment, when that user maps to a DAIV account with a valid
  authorisation. Otherwise cross-project access is refused and stated; the attached project is
  untouched.
- **Attached-project identity** (FR-002, FR-003). The acting person's identity applies *only*
  beyond the attached project. The attached project keeps the service identity, so authorship of
  published comments and merge requests, the publishing path, and the existing bot self-recognition
  are all unchanged. Two identities coexist per run, which is why FR-016 records identity per call
  rather than per run.
- **Credential source** (FR-007 to FR-010). Expanded OAuth scopes requested at sign-in, with
  renewal where the platform permits. FR-010 covers the migration: existing users hold only the
  narrower prior authorisation and keep full attached-project capability, gaining cross-project
  access after re-authorising.

**What choosing A removed**, relative to the earlier draft: the self-comment-loop risk shrank from
a P1 user story to a narrow edge case (FR-015), because only content published in *another*
project carries a person's attribution; and the sign-in story returned to P2, because a person
without authorisation can still use DAIV normally within the attached project.

**Deliberate deviations from constitutional defaults**, recorded as assumptions so planning treats
them as decisions already taken:

- Principle II asks for credentials scoped to the target repository. A per-person authorisation is
  inherently wider, because reaching other projects is the feature. The compensating control is
  that the platform's own permission checks bound every cross-project call, and the blast radius is
  limited to reads and writes the person could already perform themselves.
- Capability on a target project is governed by the acting person's platform permissions rather
  than an added DAIV-side read-only restriction.
- Code clone, commit, and push credentials are explicitly out of scope; commit authorship is
  unchanged.

**Carry into `/speckit-plan`**: FR-015 still touches the bot labels in `daiv/core/constants.py` and
the webhook callbacks, but only for projects other than the attached one. FR-014 needs the
platform-user-to-DAIV-account mapping that sign-in already establishes.
