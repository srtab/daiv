from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from codebase.base import ClientType
from codebase.conf import settings
from core.utils import build_uri, is_valid_url, url_to_data_url


class ImageURLTemplate(BaseModel):
    url: str = Field(description="URL of the image.")
    detail: str | None = Field(description="Detail of the image.", default=None)


class ImageTemplate(BaseModel):
    type: Literal["image", "text"]
    image_url: ImageURLTemplate

    @staticmethod
    def from_images(
        images: list[Image], repo_client_slug: str | None = None, project_id: int | None = None
    ) -> list[dict]:
        """
        Create a list of image templates from a list of images.

        Args:
            project_id (int): The project ID.
            images (list[Image]): The list of images.

        Returns:
            list[dict]: The list of image templates.
        """
        image_templates = []

        for image in images:
            image_url = None

            if is_valid_url(image.url):
                image_url = image.url

            elif (
                (parsed_url := urlparse(image.url))
                and not parsed_url.netloc
                and not parsed_url.scheme
                and repo_client_slug == ClientType.GITLAB
                and project_id
                and parsed_url.path.startswith("uploads/")
            ):
                _repo_image_url = build_uri(f"{settings.GITLAB_URL}api/v4/projects/{project_id}/", image.url)
                image_url = url_to_data_url(_repo_image_url, headers={"PRIVATE-TOKEN": settings.GITLAB_AUTH_TOKEN})

            if image_url:
                image_templates.append(
                    ImageTemplate(type="image", image_url=ImageURLTemplate(url=image_url)).model_dump(exclude_none=True)
                )

        return image_templates


class Image(BaseModel):
    url: str = Field(description="URL of the image.")
    filename: str = Field(description="Filename of image. Leave empty if not available.")


class ImageURLExtractorOutput(BaseModel):
    images: list[Image]
