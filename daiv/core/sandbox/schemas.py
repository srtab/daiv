from pydantic import Base64Bytes, Base64Str, BaseModel, Field, field_validator

MAX_OUTPUT_LENGTH = 2000


class StartSessionRequest(BaseModel):
    base_image: str | None = Field(default=None, description="The base image to start the session with.")
    dockerfile: str | None = Field(default=None, description="The Dockerfile to use to build the base image.")
    extract_patch: bool = Field(default=True, description="Whether to extract the patch of the changed files.")
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
    patch: Base64Str | None


class PutMutation(BaseModel):
    path: str = Field(description="Absolute path inside the sandbox, must be under /repo.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class ApplyMutationsRequest(BaseModel):
    mutations: list[PutMutation] = Field(min_length=1, max_length=64)


class MutationResult(BaseModel):
    path: str
    ok: bool
    error: str | None = None


class ApplyMutationsResponse(BaseModel):
    results: list[MutationResult]


class SeedSessionRequest(BaseModel):
    repo_archive: Base64Bytes = Field(description="Tar archive that becomes the initial state of /repo.")
