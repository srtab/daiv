from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Base64Bytes, BaseModel, Field, computed_field, field_validator, model_validator

MAX_OUTPUT_LENGTH = 2000


class StartSessionRequest(BaseModel):
    base_image: str | None = Field(default=None, description="The base image to start the session with.")
    dockerfile: str | None = Field(default=None, description="The Dockerfile to use to build the base image.")
    network_enabled: bool = Field(default=False, description="Whether to enable the network for the session.")
    memory_bytes: int | None = Field(default=None, description="Memory in bytes to be used for the session.")
    cpus: float | None = Field(default=None, description="CPUs to be used for the session.")
    environment: dict[str, str] | None = Field(
        default=None, description="Environment variables to set for the session."
    )

    @classmethod
    @field_validator("base_image", "dockerfile")
    def validate_base_image_or_dockerfile(cls, v, values):
        if not v and not values.get("dockerfile"):
            raise ValueError("Either base_image or dockerfile must be provided. Both cannot be None.")
        return v


class RunCommandsRequest(BaseModel):
    commands: list[str] = Field(description="The commands to run in the session.")
    fail_fast: bool = Field(default=True, description="Whether to fail fast if any command fails.")


class RunCommandResult(BaseModel):
    """
    The result of running a command in the sandbox.
    """

    command: str
    output: str = Field(description=f"The output of the command. Truncated to {MAX_OUTPUT_LENGTH} lines.")
    exit_code: int

    @field_validator("output")
    @classmethod
    def validate_output(cls, v):
        return "\n".join(v.split("\n")[:MAX_OUTPUT_LENGTH]) if v else ""


class RunCommandsResponse(BaseModel):
    """
    The response from running commands in the sandbox.
    """

    results: list[RunCommandResult]


class PutMutation(BaseModel):
    path: str = Field(description="Absolute path inside the sandbox, must be under /workspace/repo.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class ApplyMutationsRequest(BaseModel):
    mutations: list[PutMutation] = Field(min_length=1, max_length=64)


class MutationResult(BaseModel):
    path: str
    ok: bool
    error: str | None = None

    @model_validator(mode="after")
    def _ok_xor_error(self) -> MutationResult:
        if self.ok and self.error is not None:
            raise ValueError("MutationResult: ok=True must have error=None")
        if not self.ok and self.error is None:
            raise ValueError("MutationResult: ok=False must include an error message")
        return self


class ApplyMutationsResponse(BaseModel):
    results: list[MutationResult]


# --- /workspace file-op wire schemas -----------------------------------------
#
# Mirror of the daiv-sandbox ``Fs*`` schemas. Kept structurally identical (field
# names/types/defaults/constraints — NOT titles/descriptions, which the schema-drift
# CI test normalizes away) to the sandbox side so the schema-drift test
# (tests/unit_tests/core/sandbox/test_schema_consistency.py) passes.


class FsErrorCode(StrEnum):
    """Stable, machine-branchable fs error codes. Mirrors daiv-sandbox ``FsErrorCode``;
    the values (and their order, which the JSON-schema enum encodes) must match exactly."""

    INVALID_PATH = "invalid_path"
    NOT_FOUND = "not_found"
    NOT_A_DIRECTORY = "not_a_directory"
    IS_A_DIRECTORY = "is_a_directory"
    NOT_A_TEXT_FILE = "not_a_text_file"
    INVALID_PATTERN = "invalid_pattern"
    STRING_NOT_FOUND = "string_not_found"
    MULTIPLE_OCCURRENCES = "multiple_occurrences"
    ALREADY_EXISTS = "already_exists"
    TOO_LARGE = "too_large"
    INVALID_OFFSET = "invalid_offset"
    PERMISSION_DENIED = "permission_denied"
    EXEC_FAILED = "exec_failed"


class FsError(BaseModel):
    code: FsErrorCode = Field(description="Stable, machine-branchable error code.")
    message: str = Field(min_length=1, description="Human-readable hint the agent can act on.")


class FsLsRequest(BaseModel):
    path: str = Field(description="Absolute directory path under /workspace.")


class FsEntry(BaseModel):
    path: str = Field(description="Absolute path of the entry.")
    is_dir: bool = Field(description="Whether the entry is a directory.")


class FsLsResponse(BaseModel):
    entries: list[FsEntry] = Field(default_factory=list, description="Directory entries (empty on error).")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsReadRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    offset: int = Field(default=0, ge=0, description="0-indexed start line (text files only).")
    limit: int = Field(default=2000, ge=1, description="Maximum number of lines (text files only).")


class FsReadResponse(BaseModel):
    content: str | None = Field(
        default=None,
        description=(
            "File content (utf-8 text or base64 binary). For an empty file this is a human-readable "
            "sentinel string (with encoding 'utf-8'), not the file's bytes."
        ),
    )
    encoding: Literal["utf-8", "base64"] | None = Field(
        default=None, description="Encoding of `content`: 'utf-8' for text, 'base64' for binary."
    )
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsGrepRequest(BaseModel):
    pattern: str = Field(
        description=(
            "Regular expression to search for (Rust regex syntax, as evaluated by ripgrep). Regex is "
            "ALWAYS on — there is intentionally no literal mode, mirroring Claude Code's Grep tool. "
            "Metacharacters (e.g. `.`, `(`, `{`, `*`, `+`, `?`, `|`, `[`, `\\`) must be escaped to match "
            "them literally. When ripgrep is unavailable on the task image the search falls back to POSIX "
            "ERE (`grep -E`), whose flavor differs (libc ERE, not Rust regex)."
        )
    )
    path: str = Field(description="Absolute directory/file path under /workspace.")
    glob: str | None = Field(default=None, description="Optional filename glob to restrict the search.")
    case_insensitive: bool = Field(
        default=False, description="Match case-insensitively (ripgrep `-i` / POSIX `grep -i`)."
    )
    multiline: bool = Field(
        default=False,
        description=(
            "Allow matches to span multiple lines, letting `.` match newlines (ripgrep `--multiline`). "
            "Not supported on the POSIX fallback, where it is silently ignored."
        ),
    )
    head_limit: int | None = Field(
        default=None,
        ge=1,
        description="Cap on the number of matches returned. None means uncapped (return every match).",
    )
    exclude: list[str] = Field(
        default_factory=list,
        description="Directory basenames/globs to prune from the search (extends the server defaults).",
    )


class FsGrepMatch(BaseModel):
    path: str = Field(description="Absolute path of the matching file.")
    line: int = Field(description="1-indexed line number of the match.")
    text: str = Field(description="Text of the matching line.")


class FsGrepResponse(BaseModel):
    matches: list[FsGrepMatch] = Field(default_factory=list, description="Matches found (empty on error).")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsGlobRequest(BaseModel):
    pattern: str = Field(description="Glob pattern (supports *, **, ?, [abc]).")
    path: str = Field(description="Absolute base directory under /workspace.")
    exclude: list[str] = Field(
        default_factory=list,
        description="Directory basenames/globs to prune from the search (extends the server defaults).",
    )


class FsGlobResponse(BaseModel):
    matches: list[FsEntry] = Field(default_factory=list, description="Matching entries (empty on error).")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsWriteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(default=0o644, ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class FsWriteResponse(BaseModel):
    error: FsError | None = Field(default=None, description="Structured error; null on success.")

    # ``ok`` is derived, not stored, and serialization-only: daiv branches on ``error`` directly
    # (nothing reads ``resp.ok``), and Pydantic omits computed fields from the *validation* schema —
    # which is exactly why this matches the sandbox dump the drift test pins. Kept only for wire
    # parity with daiv-sandbox; do not reintroduce ``if resp.ok`` checks in the client/backend.
    @computed_field(description="True on success; derived from `error` (success ⇔ no error).")
    @property
    def ok(self) -> bool:
        return self.error is None


class FsEditRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    old: str = Field(description="Exact substring to replace.")
    new: str = Field(description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class FsEditResponse(BaseModel):
    occurrences: int | None = Field(default=None, description="Number of replacements made.")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsDeleteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")


class FsDeleteResponse(BaseModel):
    removed: bool = Field(
        default=False,
        description="True if a file was actually removed; False if it was already absent. "
        "Meaningful only on success (`error is None`); unspecified when `error` is set.",
    )
    error: FsError | None = Field(default=None, description="Structured error; null on success.")

    # See ``FsWriteResponse.ok``: derived, serialization-only, kept solely for wire parity.
    @computed_field(description="True on success; derived from `error` (success ⇔ no error).")
    @property
    def ok(self) -> bool:
        return self.error is None
