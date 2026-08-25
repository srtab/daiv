from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("url must be an http(s) URL")
        return value

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
