from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router

from chat.api.security import AuthBearer
from sandbox_envs.api.schemas import EnvCreate, EnvOut, EnvUpdate, EnvVar
from sandbox_envs.models import SandboxEnvironment, Scope

logger = logging.getLogger("daiv.sandbox_envs")

router = Router(auth=AuthBearer(), tags=["sandbox-envs"])


def _validation_error_response(err: ValidationError) -> tuple[int, dict]:
    """Translate a Django ``ValidationError`` into a Ninja 400 response payload."""
    payload: dict
    if hasattr(err, "message_dict"):
        payload = {"detail": "Validation failed", "errors": err.message_dict}
    else:
        payload = {"detail": "; ".join(err.messages) if err.messages else "Validation failed"}
    return 400, payload


def _user_is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False)) or bool(getattr(user, "is_staff", False))


def _visible_qs(user):
    return SandboxEnvironment.objects.filter(Q(scope=Scope.USER, user=user) | Q(scope=Scope.GLOBAL)).order_by(
        "scope", "name"
    )


def _read_env_vars(env: SandboxEnvironment) -> list[dict]:
    """Read env_vars from the encrypted column for read-only display.

    Decryption errors are logged (the descriptor already logs at exception
    level) and treated as an empty list — display paths must never crash
    because of a key rotation.
    """
    from core.encryption import DecryptionError

    try:
        return env.env_vars or []
    except DecryptionError:
        logger.error("env_vars decryption failed for SandboxEnvironment id=%s; returning empty list", env.id)
        return []


def _mask(env: SandboxEnvironment) -> EnvOut:
    rows = _read_env_vars(env)
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
    promote_default = bool(payload.is_default and payload.scope == "global")
    env = SandboxEnvironment(
        scope=Scope(payload.scope),
        user=request.auth if payload.scope == "user" else None,
        name=payload.name,
        description=payload.description,
        base_image=payload.base_image,
        network_enabled=payload.network_enabled,
        memory_bytes=payload.memory_bytes,
        cpus=payload.cpus,
        is_default=False,
    )
    env.env_vars = [v.dict() for v in payload.env_vars]
    try:
        env.full_clean()
    except ValidationError as err:
        return _validation_error_response(err)
    # Atomically: save the new row, then promote it to default if requested.
    # Going through ``promote_as_default`` guarantees no two GLOBAL envs hold
    # ``is_default=True`` simultaneously even under racing creates.
    with transaction.atomic():
        env.save()
        if promote_default:
            env.promote_as_default()
    return 201, _mask(env)


@router.patch("/{env_id}", response={200: EnvOut, 403: dict, 404: dict, 400: dict})
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
        from core.encryption import DecryptionError

        submitted = [v.dict() for v in payload.env_vars]
        try:
            existing_rows = env.env_vars or []
        except DecryptionError:
            return 400, {
                "detail": (
                    "Existing environment variables could not be decrypted. "
                    "Re-send all secret values explicitly or restore DAIV_ENCRYPTION_KEY."
                )
            }
        existing = {r["name"]: r["value"] for r in existing_rows if r.get("name")}
        merged: list[dict] = []
        for row in submitted:
            name = row.get("name")
            if row.get("is_secret") and row.get("value") in ("", "******") and name in existing:
                row = {**row, "value": existing[name]}
            merged.append(row)
        env.env_vars = merged
    try:
        env.full_clean()
    except ValidationError as err:
        return _validation_error_response(err)
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
