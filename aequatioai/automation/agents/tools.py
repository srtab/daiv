import logging
from collections.abc import Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class FunctionTool(BaseModel):
    name: str
    description: str
    fn: Callable
    parameters: list[dict[str, str | dict[str, str]]]
    required: list[str] = []

    def call(self, **kwargs):
        try:
            return self.fn(**kwargs)
        except Exception as e:
            logger.exception(e)
            return f"Error: {e}"

    def to_dict(self):
        properties = {}
        for param in self.parameters:
            properties[param["name"]] = {"type": param["type"], "description": param.get("description", "")}
            if param.get("enum"):
                properties[param["name"]]["enum"] = param["enum"]
            if param.get("items"):
                properties[param["name"]]["items"] = param["items"]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties, "required": self.required},
            },
        }


class FunctionParameter(BaseModel):
    name: str
    type: str
    description: str
    enum: list[str]
