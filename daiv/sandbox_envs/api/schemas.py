from __future__ import annotations

from typing import Literal

from ninja import Schema


class EnvVar(Schema):
    name: str
    value: str
    is_secret: bool = False


class EnvCreate(Schema):
    name: str
    description: str = ""
    scope: Literal["user", "global"] = "user"
    base_image: str
    network_enabled: bool | None = None
    memory_bytes: int | None = None
    cpus: float | None = None
    env_vars: list[EnvVar] = []
    is_default: bool = False


class EnvUpdate(Schema):
    name: str | None = None
    description: str | None = None
    base_image: str | None = None
    network_enabled: bool | None = None
    memory_bytes: int | None = None
    cpus: float | None = None
    env_vars: list[EnvVar] | None = None


class EnvOut(Schema):
    id: str
    name: str
    description: str
    scope: str
    base_image: str
    network_enabled: bool | None
    memory_bytes: int | None
    cpus: float | None
    is_default: bool
    env_vars: list[EnvVar]
