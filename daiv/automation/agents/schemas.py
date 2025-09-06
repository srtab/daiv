from __future__ import annotations

import base64
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from codebase.base import ClientType
from codebase.clients import RepoClient
from codebase.context import get_repository_ctx
from core.utils import extract_valid_image_mimetype, is_valid_url


class Image(BaseModel):
    url: str = Field(description="URL of the image.")
    filename: str = Field(description="Filename of image. Leave empty if not available.")


class ImageTemplate(BaseModel):
    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"]
    data: str | None = None
    url: str | None = None
    mime_type: str | None = None

    @staticmethod
    async def from_images(images: list[Image]) -> list[ImageTemplate]:
        """
        Create a list of image templates from a list of images.

        Args:
            images (list[Image]): The list of images.

        Returns:
            list[ImageTemplate]: The list of image templates.
        """
        ctx = get_repository_ctx()
        repo_client = RepoClient.create_instance()
        image_templates = []

        for image in images:
            if is_valid_url(image.url):
                image_templates.append(ImageTemplate(source_type="url", url=image.url).model_dump(exclude_none=True))

            elif (
                (parsed_url := urlparse(image.url))
                and not parsed_url.netloc
                and not parsed_url.scheme
                and repo_client.client_slug == ClientType.GITLAB
                and parsed_url.path.startswith(("/uploads/", "uploads/"))
                and (mime_type := extract_valid_image_mimetype(image.url))
            ):
                image_content = await repo_client.get_project_uploaded_file(ctx.repo_id, image.url)

                image_templates.append(
                    ImageTemplate(
                        source_type="base64", data=base64.b64encode(image_content).decode(), mime_type=mime_type
                    ).model_dump(exclude_none=True)
                )

        return image_templates
