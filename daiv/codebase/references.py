from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
_URL_CHARSET_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+$")

# Separators that turn a bare identifier into a cross-project platform reference.
_CROSS_PROJECT_SEPARATORS = frozenset("/#")


@dataclass(frozen=True)
class ExternalRef:
    key: str
    provider: str = PROVIDER_GENERIC
    url: str = ""
    relation: RefRelation = "relates"


class RefIn(BaseModel):
    """Intake validation for one caller-declared external reference."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$")
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
        """A ``closes`` key is emitted unanchored behind a closing keyword (``Fixes <key>``) in the
        MR description and the commit message, so a ``namespace/project#7`` key would make DAIV
        close an issue in any project its bot can reach.
        """
        if self.relation == "closes" and _CROSS_PROJECT_SEPARATORS & set(self.key):
            raise ValueError("a closes reference key may not contain '/' or '#'")
        return self

    def to_stored(self) -> dict:
        return {"key": self.key, "provider": self.provider, "url": self.url, "relation": self.relation}


def refs_from_stored(raw: object) -> tuple[ExternalRef, ...]:
    """Coerce ``Session.external_refs`` JSON into refs; a malformed entry is skipped, never fatal."""
    if not isinstance(raw, list):
        return ()
    refs: list[ExternalRef] = []
    for item in raw:
        try:
            refs.append(ExternalRef(**RefIn(**item).model_dump()))
        except ValidationError, TypeError:
            logger.warning("Skipping malformed stored external ref: %r", item)
    return tuple(refs)


def merge_stored_refs(existing: list, new: list) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in [*existing, *new]:
        if not isinstance(item, dict):
            continue
        ident = (str(item.get("provider", "")), str(item.get("key", "")))
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(item)
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


def assemble_run_references(
    declared: Sequence[ExternalRef] | None, *, scope: Scope | None, issue: Issue | None, git_platform: GitPlatform
) -> tuple[ExternalRef, ...]:
    """Final per-run reference set: caller-declared refs plus the platform issue ref an
    issue-scoped run closes (preserving the webhook flow's auto-close behavior)."""
    refs = tuple(declared or ())
    provider = _ISSUE_PROVIDER_BY_PLATFORM.get(git_platform)
    if scope == Scope.ISSUE and issue is not None and issue.iid is not None and provider:
        refs = (*refs, ExternalRef(key=str(issue.iid), provider=provider, relation="closes"))
    return dedupe_refs(refs)


def _issue_line(ref: ExternalRef, repo_slug: str, *, gitlab: bool) -> str:
    if ref.relation == "closes":
        return f"Closes: {repo_slug}#{ref.key}{'+' if gitlab else ''}"
    return f"Related to {repo_slug}#{ref.key}"


def render_references_block(refs: Sequence[ExternalRef], *, repo_slug: str) -> str:
    """MR-description footer for ``refs``; platform-issue refs stay standalone (the platforms parse
    closing keywords there), everything else groups under one References heading."""
    standalone: list[str] = []
    bullets: list[str] = []
    for ref in refs:
        if ref.provider == PROVIDER_GITLAB_ISSUE:
            standalone.append(_issue_line(ref, repo_slug, gitlab=True))
        elif ref.provider == PROVIDER_GITHUB_ISSUE:
            standalone.append(_issue_line(ref, repo_slug, gitlab=False))
        elif ref.provider == PROVIDER_SENTRY and ref.relation == "closes":
            bullets.append(f"- Fixes {ref.key}" + (f" ([Sentry]({ref.url}))" if ref.url else ""))
        else:
            bullets.append(f"- [{ref.key}]({ref.url})" if ref.url else f"- {ref.key}")
    parts = standalone
    if bullets:
        parts = [*standalone, "**References:**", *bullets]
    return "\n".join(parts)


def render_commit_trailers(refs: Sequence[ExternalRef]) -> tuple[str, ...]:
    trailers: list[str] = []
    for ref in refs:
        if ref.provider == PROVIDER_SENTRY and ref.relation == "closes":
            trailers.append(f"Fixes {ref.key}")
        elif ref.provider == PROVIDER_JIRA:
            trailers.append(f"Refs: {ref.key}")
    return tuple(trailers)
