from __future__ import annotations

from django.db.models import Q
from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router

from chat.api.security import AuthBearer
from sandbox_envs.api.schemas import EnvCreate, EnvOut, EnvUpdate, EnvVar
from sandbox_envs.models import SandboxEnvironment, Scope

router = Router(auth=AuthBearer(), tags=["sandbox-envs"])


def _user_is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False)) or bool(getattr(user, "is_staff", False))


def _visible_qs(user):
    return SandboxEnvironment.objects.filter(Q(scope=Scope.USER, user=user) | Q(scope=Scope.GLOBAL)).order_by(
        "scope", "name"
    )


def _mask(env: SandboxEnvironment) -> EnvOut:
    rows = env.env_vars or []
    return EnvOut(
        id=str(env.id),
        name=env.name,
        description=env.description,
        scope=env.scope,
        base_image=env.base_image,
        network_enabled=env.network_enabled,
        memory_bytes=env.memory_bytes,
        cpus=float(env.cpus) if env.cpus is not None else None,
        is_default=env.is_default,
        env_vars=[
            EnvVar(
                name=r["name"], value="******" if r.get("is_secret") else r["value"], is_secret=bool(r.get("is_secret"))
            )
            for r in rows
        ],
    )


@router.get("/", response=list[EnvOut])
def list_envs(request: HttpRequest):
    return [_mask(env) for env in _visible_qs(request.auth)]


@router.post("/", response={201: EnvOut, 403: dict, 400: dict})
def create_env(request: HttpRequest, payload: EnvCreate):
    if payload.scope == "global" and not _user_is_admin(request.auth):
        return 403, {"detail": "Admin required for global environments"}
    env = SandboxEnvironment(
        scope=Scope(payload.scope),
        user=request.auth if payload.scope == "user" else None,
        name=payload.name,
        description=payload.description,
        base_image=payload.base_image,
        network_enabled=payload.network_enabled,
        memory_bytes=payload.memory_bytes,
        cpus=payload.cpus,
        is_default=(payload.is_default and payload.scope == "global"),
    )
    env.env_vars = [v.dict() for v in payload.env_vars]
    env.full_clean()
    env.save()
    return 201, _mask(env)


@router.patch("/{env_id}", response={200: EnvOut, 403: dict, 404: dict})
def update_env(request: HttpRequest, env_id: str, payload: EnvUpdate):
    env = _visible_qs(request.auth).filter(pk=env_id).first()
    if env is None:
        return 404, {"detail": "Not found"}
    if env.scope == Scope.GLOBAL and not _user_is_admin(request.auth):
        return 403, {"detail": "Admin required"}
    for field in ("name", "description", "base_image", "network_enabled", "memory_bytes", "cpus"):
        value = getattr(payload, field)
        if value is not None:
            setattr(env, field, value)
    if payload.env_vars is not None:
        submitted = [v.dict() for v in payload.env_vars]
        existing = {r["name"]: r["value"] for r in (env.env_vars or []) if r.get("name")}
        merged: list[dict] = []
        for row in submitted:
            name = row.get("name")
            if row.get("is_secret") and row.get("value") in ("", "******") and name in existing:
                row = {**row, "value": existing[name]}
            merged.append(row)
        env.env_vars = merged
    env.full_clean()
    env.save()
    return 200, _mask(env)


@router.delete("/{env_id}", response={204: None, 403: dict, 404: dict, 409: dict})
def delete_env(request: HttpRequest, env_id: str):
    env = _visible_qs(request.auth).filter(pk=env_id).first()
    if env is None:
        return 404, {"detail": "Not found"}
    if env.scope == Scope.GLOBAL and not _user_is_admin(request.auth):
        return 403, {"detail": "Admin required"}
    if env.is_default:
        return 409, {"detail": "Cannot delete the global default"}
    env.delete()
    return 204, None


@router.post("/{env_id}/set-default", response={200: EnvOut, 403: dict, 404: dict})
def set_default(request: HttpRequest, env_id: str):
    if not _user_is_admin(request.auth):
        return 403, {"detail": "Admin required"}
    env = SandboxEnvironment.objects.filter(pk=env_id, scope=Scope.GLOBAL).first()
    if env is None:
        return 404, {"detail": "Not found"}
    env.promote_as_default()
    return 200, _mask(env)
