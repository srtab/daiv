from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel

DEFAULT_BRANCH_NAME = "daiv/changes"

_DISALLOWED = re.compile(r"[^a-z0-9._/-]+")


def normalize_branch_name(value: str) -> str:
    """Coerce a model-proposed branch name into a valid git branch name.

    Normalizes rather than rejects: the branch name is cosmetic metadata DAIV owns
    downstream (deduped by ``unique_branch_name``, shell-quoted at push), so a bad
    value must never raise and abort the publish. Dots are kept — they are git-legal,
    so ``chore/release-1.0.0`` survives verbatim (the Sentry DAIV-AV input).
    """
    name = _DISALLOWED.sub("-", value.strip().lower())
    name = re.sub(r"\.{2,}", ".", name)
    name = re.sub(r"/{2,}", "/", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"\.lock$", "", name)
    return name.strip("./-") or DEFAULT_BRANCH_NAME


class CommitMetadata(BaseModel):
    commit_message: str


class PullRequestMetadata(BaseModel):
    title: str
    branch: Annotated[str, AfterValidator(normalize_branch_name)]
    description: str
