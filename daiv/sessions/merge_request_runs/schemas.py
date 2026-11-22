"""The ``merge_request`` field of a jobs request's ``repos`` entry, shared by the jobs API and the MCP endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MERGE_REQUEST_ALONE = "merge_request can't be combined with ref or a per-repository prompt"


class MergeRequestTarget(BaseModel):
    """A ``repos`` entry that may name a merge request in place of its ``ref`` and per-repository ``prompt``, which
    the model it is mixed into declares."""

    merge_request: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The open merge request (GitLab iid, GitHub number) to run on, in place of `ref`: the run goes where a "
            "mention by you would, and its final message is posted on the merge request. Refused with `ref`, a "
            "per-repository `prompt`, `shared_session`, `thread_id` or another `repos` entry."
        ),
    )

    @model_validator(mode="after")
    def _merge_request_alone(self):
        if self.merge_request is not None and (
            getattr(self, "ref", None) is not None or getattr(self, "prompt", None) is not None
        ):
            raise ValueError(MERGE_REQUEST_ALONE)
        return self
