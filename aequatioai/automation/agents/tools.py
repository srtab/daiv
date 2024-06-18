import logging
from collections.abc import Callable
from functools import cached_property

from instructor import OpenAISchema
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class FunctionTool(BaseModel):
    schema_model: type[OpenAISchema]
    fn: Callable

    def call(self, **kwargs):
        try:
            return self.fn(**kwargs)
        except Exception as e:
            logger.exception(e)
            return f"Error: {e}"

    def to_schema(self) -> dict[str, dict | str]:
        return {"type": "function", "function": self.schema_model.openai_schema}

    @cached_property
    def name(self) -> str:
        return self.schema_model.openai_schema["name"]
