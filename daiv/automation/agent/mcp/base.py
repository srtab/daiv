from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .schemas import ToolFilter  # noqa: TC001

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection


class MCPServer(ABC):
    """Base class for code-defined (built-in) MCP servers."""

    name: str
    tool_filter: ToolFilter | None = None

    def is_enabled(self) -> bool:
        """Default: enabled only if the DB-side ``MCPServer`` row exists and
        has ``enabled=True``. Subclasses may override (e.g. to also check a
        URL env var is set), in which case they should call
        ``super().is_enabled()`` to keep the DB toggle honored."""
        return self._db_enabled()

    @classmethod
    def _db_enabled(cls) -> bool:
        try:
            from django.db.utils import ProgrammingError

            from mcp_servers.models import MCPServer as DBModel
        except ImportError:
            return True
        try:
            return DBModel.objects.filter(name=cls.name, enabled=True).exists()
        except ProgrammingError:
            # Table missing (pre-migration) → enabled. OperationalError is NOT caught:
            # swallowing it would flip every disabled built-in back on during a DB outage.
            return True

    @abstractmethod
    def get_connection(self) -> Connection: ...
