from __future__ import annotations

import base64
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from codebase.base import ClientType
from codebase.conf import settings
from core.utils import async_download_url, build_uri, extract_valid_image_mimetype, is_valid_url


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
    async def from_images(
        images: list[Image], repo_client_slug: ClientType | None = None, project_id: int | None = None
    ) -> list[ImageTemplate]:
        """
        Create a list of image templates from a list of images.

        Args:
            images (list[Image]): The list of images.
            repo_client_slug (ClientType): The repository client slug.
            project_id (int): The project ID.

        Returns:
            list[ImageTemplate]: The list of image templates.
        """
        image_templates = []

        for image in images:
            if is_valid_url(image.url):
                image_templates.append(ImageTemplate(source_type="url", url=image.url).model_dump(exclude_none=True))

            elif (
                (parsed_url := urlparse(image.url))
                and not parsed_url.netloc
                and not parsed_url.scheme
                and repo_client_slug == ClientType.GITLAB
                and project_id
                and parsed_url.path.startswith(("/uploads/", "uploads/"))
            ):
                assert settings.GITLAB_AUTH_TOKEN is not None, "GitLab auth token is not set"

                _repo_image_url = build_uri(f"{settings.GITLAB_URL}api/v4/projects/{project_id}/", image.url)

                if mime_type := extract_valid_image_mimetype(_repo_image_url):
                    image_content = await async_download_url(
                        _repo_image_url, headers={"PRIVATE-TOKEN": settings.GITLAB_AUTH_TOKEN.get_secret_value()}
                    )
                    image_templates.append(
                        ImageTemplate(
                            source_type="base64", data=base64.b64encode(image_content).decode(), mime_type=mime_type
                        ).model_dump(exclude_none=True)
                    )

        return image_templates
