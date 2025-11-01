from pydantic import Base64Str, BaseModel, Field, field_validator

MAX_OUTPUT_LENGTH = 2000


class StartSessionRequest(BaseModel):
    base_image: str | None = Field(default=None, description="The base image to start the session with.")
    dockerfile: str | None = Field(default=None, description="The Dockerfile to use to build the base image.")

    @classmethod
    @field_validator("base_image", "dockerfile")
    def validate_base_image_or_dockerfile(cls, v, values):
        if not v and not values.get("dockerfile"):
            raise ValueError("Either base_image or dockerfile must be provided. Both cannot be None.")
        return v


class RunCommandsRequest(BaseModel):
    commands: list[str] = Field(description="The commands to run in the session.")
    workdir: str | None = Field(default=None, description="The working directory to use for the commands.")
    archive: str = Field(description="The archive to use as the working directory for the commands.")
    extract_patch: bool = Field(default=True, description="Whether to extract the patch of the changed files.")
    fail_fast: bool = Field(default=True, description="Whether to fail fast if any command fails.")


class RunCommandResult(BaseModel):
    """
    The result of running a command in the sandbox.
    """

    command: str
    output: str = Field(description="The output of the command. Truncated to 10000 characters.")
    exit_code: int

    @field_validator("output")
    @classmethod
    def validate_output(cls, v):
        return v[:MAX_OUTPUT_LENGTH]


class RunCommandsResponse(BaseModel):
    """
    The response from running commands in the sandbox.
    """

    results: list[RunCommandResult]
    patch: Base64Str | None
