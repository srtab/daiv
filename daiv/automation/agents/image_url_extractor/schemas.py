from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from codebase.conf import settings
from core.utils import build_uri, is_valid_url, url_to_data_url


class ImageURLTemplate(BaseModel):
    url: str = Field(description="URL of the image.")
    path: str = Field(description="Path of the image.")
    detail: str | None = Field(description="Detail of the image.", default=None)


class ImageTemplate(BaseModel):
    type: Literal["image", "text"]
    image_url: ImageURLTemplate

    @staticmethod
    def from_images(project_id: int, images: list[Image]) -> list[dict]:
        image_templates = []

        for image in images:
            image_template = None

            if is_valid_url(image.url):
                image_template = ImageTemplate(
                    type="image", image_url=ImageURLTemplate(url=image.url, path=image.filename)
                ).model_dump(exclude_none=True)

            elif (parsed_url := urlparse(image.url)) and not parsed_url.netloc and not parsed_url.scheme:
                image_url = build_uri(f"{settings.CODEBASE_GITLAB_URL}/api/v4/projects/{project_id}/", image.url)
                if image_data_url := url_to_data_url(
                    image_url, headers={"PRIVATE-TOKEN": settings.CODEBASE_GITLAB_AUTH_TOKEN}
                ):
                    image_template = ImageTemplate(
                        type="image", image_url=ImageURLTemplate(url=image_data_url, path=image.filename)
                    ).model_dump(exclude_none=True)

            if image_template:
                image_templates.append(image_template)

        return image_templates


class Image(BaseModel):
    url: str = Field(description="URL of the image.")
    filename: str = Field(description="Filename of image. Leave empty if not available.")


class ImageURLExtractorOutput(BaseModel):
    images: list[Image]
