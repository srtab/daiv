from typing import Annotated

from pydantic import BaseModel, Field


class ExtractedPaths(BaseModel):
    paths: list[str] = Field(description="Valid filesystem paths found in the code snippet.")


class PathReplacement(BaseModel):
    original_path: str = Field(description="Path to be replaced.")
    new_path: str = Field(description="Replacement path.")


class PathsToReplace(BaseModel):
    paths: list[PathReplacement] = Field(description="List of paths to be replaced and their respective replacements.")
