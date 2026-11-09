"""Create the demo project the README recordings use, in the local GitLab.

Run from the repository root while the local GitLab and a DAIV stack are up:

    uv run --with python-gitlab python scripts/demo/seed_gitlab.py --hook-url https://daiv-9-app-1:8000/api/codebase/callbacks/gitlab/

It is idempotent: an existing user, group, project, label or hook is reused, and the
code is pushed only to an empty project. ``--reset`` deletes the project and creates it
again under a new path (``inventory-api-<n>``, still named ``inventory-api``): DAIV keys
its sessions on project path and issue number, so a project recreated at the same path
would resume the deleted project's conversations. The current path is kept in
``~/.cache/daiv-demo/project`` for ``record_hero.py``.
"""

import argparse
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import gitlab

GITLAB_URL = "http://127.0.0.1:8929"
STATE_DIR = Path.home() / ".cache" / "daiv-demo"
DEMO_REPO = Path(__file__).parent / "demo-repo"

USERNAME = "alex"
USER_NAME = "Alex Morgan"
GROUP = "acme"
PROJECT_NAME = "inventory-api"
PROJECT_FILE = STATE_DIR / "project"
BOT_USERNAME = "daiv"


def admin_client() -> gitlab.Gitlab:
    token = (STATE_DIR / "admin-token").read_text().strip()
    gl = gitlab.Gitlab(GITLAB_URL, private_token=token)
    gl.auth()
    return gl


def ensure_user(gl: gitlab.Gitlab):
    if users := gl.users.list(username=USERNAME):
        return users[0]
    password = secrets.token_urlsafe(18)
    user = gl.users.create({
        "username": USERNAME,
        "name": USER_NAME,
        "email": f"{USERNAME}@example.com",
        "password": password,
        "skip_confirmation": True,
    })
    password_file = STATE_DIR / f"{USERNAME}-password"
    password_file.write_text(password)
    password_file.chmod(0o600)
    return user


def ensure_group(gl: gitlab.Gitlab, owner):
    try:
        group = gl.groups.get(GROUP)
    except gitlab.GitlabGetError:
        group = gl.groups.create({"name": "Acme", "path": GROUP, "visibility": "private"})
    if not any(m.id == owner.id for m in group.members.list(get_all=True)):
        group.members.create({"user_id": owner.id, "access_level": gitlab.const.AccessLevel.OWNER})
    return group


def delete_permanently(gl: gitlab.Gitlab, project) -> None:
    if not project.attributes.get("marked_for_deletion_on"):
        project.delete()
    scheduled = gl.projects.get(project.id)
    gl.http_delete(
        f"/projects/{project.id}", query_data={"permanently_remove": True, "full_path": scheduled.path_with_namespace}
    )
    while True:
        try:
            gl.projects.get(project.id)
        except gitlab.GitlabGetError:
            return
        time.sleep(2)


def current_path() -> str | None:
    return PROJECT_FILE.read_text().strip() if PROJECT_FILE.exists() else None


def next_path(previous: str | None) -> str:
    suffix = previous.rsplit("-", 1)[-1] if previous else ""
    number = int(suffix) + 1 if suffix.isdigit() else 1
    return f"{GROUP}/{PROJECT_NAME}-{number}"


def find_project(gl: gitlab.Gitlab, path: str | None):
    if path is None:
        return None
    try:
        return gl.projects.get(path)
    except gitlab.GitlabGetError:
        return None


def ensure_project(gl: gitlab.Gitlab, group, reset: bool):
    path = current_path()
    project = find_project(gl, path)
    if reset or project is None:
        if project is not None:
            delete_permanently(gl, project)
        path = next_path(path)
        project = gl.projects.create({
            "name": PROJECT_NAME,
            "path": path.split("/", 1)[1],
            "namespace_id": group.id,
            "description": "Warehouse items and stock levels.",
            "visibility": "private",
            "initialize_with_readme": False,
        })
        PROJECT_FILE.write_text(path)
    return project


def ensure_member(project, user, level):
    if not any(m.id == user.id for m in project.members.list(get_all=True)):
        project.members.create({"user_id": user.id, "access_level": level})


def push_code(project, token: str):
    if project.empty_repo is False:
        return
    work = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(DEMO_REPO, work, dirs_exist_ok=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": USER_NAME,
            "GIT_AUTHOR_EMAIL": f"{USERNAME}@example.com",
            "GIT_COMMITTER_NAME": USER_NAME,
            "GIT_COMMITTER_EMAIL": f"{USERNAME}@example.com",
        }
        remote = f"http://oauth2:{token}@127.0.0.1:8929/{project.path_with_namespace}.git"
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "--no-verify", "-m", "Inventory API with item CRUD"],
            ["git", "push", "-q", remote, "main"],
        ):
            subprocess.run(cmd, cwd=work, env=env, check=True)
    finally:
        shutil.rmtree(work)


def ensure_labels(project):
    existing = {label.name for label in project.labels.list(get_all=True)}
    for name, color in (("daiv", "#6E49CB"), ("enhancement", "#1F75CB"), ("bug", "#DC143C")):
        if name not in existing:
            project.labels.create({"name": name, "color": color})


def ensure_hook(project, url: str, secret: str):
    data = {
        "url": url,
        "name": "DAIV",
        "push_events": True,
        "issues_events": True,
        "note_events": True,
        "merge_requests_events": True,
        "pipeline_events": True,
        "enable_ssl_verification": False,
        "token": secret,
    }
    hooks = project.hooks.list(get_all=True)
    for hook in hooks:
        hook.delete()
    project.hooks.create(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hook-url", required=True, help="DAIV's GitLab callback URL, as the GitLab container reaches it"
    )
    parser.add_argument("--app-container", default="daiv-9-app-1", help="container to read the webhook secret from")
    parser.add_argument("--reset", action="store_true", help="delete the project and create it under a new path")
    args = parser.parse_args()

    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    gl = admin_client()
    secret = subprocess.run(
        ["docker", "exec", args.app_container, "printenv", "CODEBASE_GITLAB_WEBHOOK_SECRET"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    alex = ensure_user(gl)
    group = ensure_group(gl, alex)
    project = ensure_project(gl, group, args.reset)
    bot = gl.users.list(username=BOT_USERNAME)[0]
    ensure_member(project, bot, gitlab.const.AccessLevel.MAINTAINER)
    push_code(gl.projects.get(project.id), gl.private_token)
    ensure_labels(project)
    ensure_hook(project, args.hook_url, secret)
    print(f"{GITLAB_URL}/{project.path_with_namespace} ready (project id {project.id})")


if __name__ == "__main__":
    main()
