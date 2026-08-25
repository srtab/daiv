from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codebase.base import GitPlatform, Scope

if TYPE_CHECKING:
    from collections.abc import Sequence

    from codebase.base import Issue

logger = logging.getLogger("daiv.codebase")

PROVIDER_GENERIC = "generic"
PROVIDER_GITLAB_ISSUE = "gitlab-issue"
PROVIDER_GITHUB_ISSUE = "github-issue"
PROVIDER_SENTRY = "sentry"
PROVIDER_JIRA = "jira"

MAX_REFS_PER_SUBMISSION = 20
MAX_REFS_PER_SESSION = 50

RefRelation = Literal["closes", "relates"]

# RFC 3986's URI character set minus the parentheses, which delimit a markdown link destination.
_URL_CHARSET_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+\Z")

# ``\z`` rather than ``$``, which matches before a trailing newline too — and a key ending in one
# would break the commit trailer and the description line it gets rendered into.
_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/#-]*\z"

# Separators that turn a bare identifier into a cross-project platform reference.
_CROSS_PROJECT_SEPARATORS = frozenset("/#")


class RefIn(BaseModel):
    """Intake validation for one caller-declared external reference."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=_KEY_PATTERN)
    provider: str = Field(default=PROVIDER_GENERIC, pattern=r"^[a-z0-9-]{1,32}$")
    url: str = Field(default="", max_length=500)
    relation: RefRelation = "relates"

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        """Renderers embed the url in a markdown link destination inside the MR description, so a
        space, a control character or a ``)`` would end the destination and turn the rest of the
        value into body text the platforms parse (a forged ``Closes:`` line closes someone's issue).
        """
        if not value:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be an http(s) URL")
        if not _URL_CHARSET_RE.match(value):
            raise ValueError("url must percent-encode anything outside the RFC 3986 charset, parentheses included")
        return value

    @model_validator(mode="after")
    def _closing_key_is_a_bare_identifier(self) -> RefIn:
        """A ``closes`` key can be emitted behind a closing keyword (Sentry's ``Fixes <key>``),
        and ``provider`` is caller-chosen so validation cannot lean on rendering: a
        ``namespace/project#7`` key there would close an issue in any project the bot can reach.
        """
        if self.relation == "closes" and _CROSS_PROJECT_SEPARATORS & set(self.key):
            raise ValueError("a closes reference key may not contain '/' or '#'")
        return self

    def to_stored(self) -> dict[str, str]:
        return self.model_dump()


class ExternalRef(RefIn):
    """A reference as a run carries it: the intake invariants, frozen so it can ride inside the
    immutable ``RuntimeCtx``. Inheriting them is what keeps every constructor path — including the
    stored-JSON one — from handing the renderers a forged closing keyword or a link-breaking value.
    """

    model_config = ConfigDict(frozen=True)


def refs_from_stored(raw: object) -> tuple[ExternalRef, ...]:
    """Coerce ``Session.external_refs`` JSON into refs; a malformed entry is skipped, never fatal,
    and unknown fields (additive schema evolution) are ignored instead of dropping the entry."""
    if not isinstance(raw, list):
        logger.warning("Ignoring malformed stored external refs column: %r", raw)
        return ()
    refs: list[ExternalRef] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("Skipping malformed stored external ref: %r", item)
            continue
        try:
            refs.append(ExternalRef(**{k: v for k, v in item.items() if k in ExternalRef.model_fields}))
        except ValueError, TypeError:
            logger.warning("Skipping malformed stored external ref: %r", item)
    return tuple(refs)


def merge_stored_refs(existing: list[dict], new: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in [*existing, *new]:
        if not isinstance(item, dict):
            logger.warning("Skipping malformed stored external ref: %r", item)
            continue
        ident = (str(item.get("provider", "")), str(item.get("key", "")))
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(item)
    if len(merged) > MAX_REFS_PER_SESSION:
        logger.warning(
            "Session reference budget of %d reached; dropping the %d newest reference(s)",
            MAX_REFS_PER_SESSION,
            len(merged) - MAX_REFS_PER_SESSION,
        )
    return merged[:MAX_REFS_PER_SESSION]


def dedupe_refs(refs: Sequence[ExternalRef]) -> tuple[ExternalRef, ...]:
    out: list[ExternalRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if (ref.provider, ref.key) in seen:
            continue
        seen.add((ref.provider, ref.key))
        out.append(ref)
    return tuple(out)


_ISSUE_PROVIDER_BY_PLATFORM = {GitPlatform.GITLAB: PROVIDER_GITLAB_ISSUE, GitPlatform.GITHUB: PROVIDER_GITHUB_ISSUE}

# Providers the git platform itself parses out of the MR description; every renderer branches on
# this set rather than re-listing its members.
PLATFORM_ISSUE_PROVIDERS = frozenset(_ISSUE_PROVIDER_BY_PLATFORM.values())


def assemble_run_references(
    declared: Sequence[ExternalRef] | None, *, scope: Scope | None, issue: Issue | None, git_platform: GitPlatform
) -> tuple[ExternalRef, ...]:
    """Final per-run reference set: the platform issue ref an issue-scoped run closes, then the
    caller-declared refs. The derived ref leads so a declared duplicate cannot demote the
    webhook flow's auto-close to ``relates`` through first-wins dedupe."""
    refs = tuple(declared or ())
    provider = _ISSUE_PROVIDER_BY_PLATFORM.get(git_platform)
    if scope == Scope.ISSUE and issue is not None and issue.iid is not None and provider:
        refs = (ExternalRef(key=str(issue.iid), provider=provider, relation="closes"), *refs)
    return dedupe_refs(refs)


def _issue_line(ref: ExternalRef, repo_slug: str, *, gitlab: bool) -> str:
    if ref.relation == "closes":
        return f"Closes: {repo_slug}#{ref.key}{'+' if gitlab else ''}"
    return f"Related to {repo_slug}#{ref.key}"


def render_references_block(refs: Sequence[ExternalRef], *, repo_slug: str) -> str:
    """MR-description footer for ``refs``; platform-issue refs stay standalone to keep the legacy
    issue footer byte-identical, everything else groups under one References heading."""
    standalone: list[str] = []
    bullets: list[str] = []
    for ref in refs:
        if ref.provider in PLATFORM_ISSUE_PROVIDERS:
            standalone.append(_issue_line(ref, repo_slug, gitlab=ref.provider == PROVIDER_GITLAB_ISSUE))
        elif ref.provider == PROVIDER_SENTRY and ref.relation == "closes":
            bullets.append(f"- Fixes {ref.key}" + (f" ([Sentry]({ref.url}))" if ref.url else ""))
        else:
            bullets.append(f"- [{ref.key}]({ref.url})" if ref.url else f"- {ref.key}")
    parts = standalone
    if bullets:
        parts = [*standalone, "**References:**", *bullets]
    return "\n".join(parts)


def render_agent_context(refs: Sequence[ExternalRef]) -> str:
    """The refs as context for the metadata-writing model, or ``""`` when none apply.

    Platform issue refs are left out: an issue-scoped run already describes its issue in full,
    so repeating the iid here only invites the model to write a second closing line.
    """
    lines = [
        f"- {ref.provider}: {ref.key}" + (f" ({ref.url})" if ref.url else "") + f" [{ref.relation}]"
        for ref in refs
        if ref.provider not in PLATFORM_ISSUE_PROVIDERS
    ]
    if not lines:
        return ""
    return "External work items this change addresses:\n" + "\n".join(lines)


def render_commit_trailers(refs: Sequence[ExternalRef]) -> tuple[str, ...]:
    trailers: list[str] = []
    for ref in refs:
        if ref.provider == PROVIDER_SENTRY and ref.relation == "closes":
            trailers.append(f"Fixes {ref.key}")
        elif ref.provider == PROVIDER_JIRA:
            trailers.append(f"Refs: {ref.key}")
    return tuple(trailers)
