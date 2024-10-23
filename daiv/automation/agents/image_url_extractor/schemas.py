from pydantic import BaseModel, Field


class Image(BaseModel):
    url: str = Field(description="URL of the image.")
    filename: str = Field(description="Filename of image. Leave empty if not available.")


class ImageURLExtractorOutput(BaseModel):
    images: list[Image]
