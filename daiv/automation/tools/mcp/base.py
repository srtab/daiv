from abc import ABC

from langchain_mcp_adapters.sessions import SSEConnection


class MCPServer(ABC):
    name: str
    connection: SSEConnection

    def is_enabled(self) -> bool:
        return True
