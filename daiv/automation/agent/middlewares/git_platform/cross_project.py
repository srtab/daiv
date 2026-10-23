from __future__ import annotations

import argparse
import functools
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlparse

from accounts.credentials import CredentialReason, ainvalidate_cached_token, aresolve_access_token, platform_host
from codebase.base import GitPlatform
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL, CROSS_PROJECT_CONTENT_MARKER
from core.constants import CrossProjectOutcome as _Outcome

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime

    from automation.agent.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")


# Refused outside the attached project although the person's token carries them, because issue or comment
# text somebody else wrote can choose what they are spent on. Reads and creating an issue or note still cross.
GITLAB_CROSS_PROJECT_DENIED_ACTIONS: dict[str, frozenset[str]] = {
    "project": frozenset({"delete-merged-branches", "trigger-pipeline"}),
    # ``move`` relocates an issue out of the target project; the note ``update`` verbs overwrite
    # text somebody else wrote, which a maintainer-level token is entitled to do.
    "project-issue": frozenset({"move", "reset-spent-time", "reset-time-estimate"}),
    "project-merge-request": frozenset({"create", "reset-spent-time", "reset-time-estimate"}),
    "project-issue-note": frozenset({"update"}),
    "project-issue-discussion-note": frozenset({"update"}),
    "project-merge-request-note": frozenset({"update"}),
    "project-merge-request-discussion": frozenset({"update"}),
    "project-merge-request-discussion-note": frozenset({"update"}),
    "project-label": frozenset({"create", "update"}),
    "project-snippet": frozenset({"create", "update"}),
    "project-snippet-discussion": frozenset({"update"}),
    "project-snippet-discussion-note": frozenset({"update"}),
    "project-snippet-note": frozenset({"update"}),
    "project-pipeline": frozenset({"create", "cancel", "retry"}),
    "project-merge-request-pipeline": frozenset({"create"}),
    "project-job": frozenset({"retry", "play"}),
    "project-branch": frozenset({"create"}),
    "project-tag": frozenset({"create"}),
    "project-release": frozenset({"create", "update"}),
    "project-release-link": frozenset({"create", "update"}),
    "project-issue-link": frozenset({"delete"}),
    "project-issue-award-emoji": frozenset({"delete"}),
    "project-merge-request-award-emoji": frozenset({"delete"}),
    "project-merge-request-note-award-emoji": frozenset({"delete"}),
    "project-merge-request-draft-note": frozenset({"delete"}),
    "project-snippet-award-emoji": frozenset({"delete"}),
    "project-snippet-note-award-emoji": frozenset({"delete"}),
}

GITHUB_CROSS_PROJECT_DENIED_ACTIONS: dict[str, frozenset[str]] = {
    "issue": frozenset({"close", "reopen", "lock", "unlock", "develop"}),
    # ``review`` includes --approve, which can satisfy a required review and release auto-merge.
    "pr": frozenset({"create", "close", "reopen", "lock", "unlock", "review"}),
    "workflow": frozenset({"run"}),
    "run": frozenset({"rerun"}),
    "release": frozenset({"create", "edit", "upload"}),
    "cache": frozenset({"delete"}),
    "label": frozenset({"create", "edit"}),
}

REFUSAL_DISABLED = "error: Cross-project access is not enabled on this DAIV deployment. Only {attached} can be reached."
REFUSAL_NO_ACTING_USER = (
    "error: This run has no requesting user, so only {attached} can be reached. Cross-project access "
    "needs a run started by a signed-in user, or by a platform user with a linked DAIV account."
)
REFUSAL_NO_CREDENTIAL = (
    "error: {person} has not authorised DAIV to access other projects on {provider}. They can "
    "authorise it from their DAIV account settings."
)
REFUSAL_EXPIRED = (
    "error: {person}'s {provider} authorisation has expired and could not be renewed. They need to "
    "re-authorise from their DAIV account settings."
)
REFUSAL_REVOKED = (
    "error: {person}'s {provider} authorisation was revoked. They need to re-authorise from their "
    "DAIV account settings."
)
REFUSAL_CREDENTIAL_REJECTED = (
    "error: {provider} rejected {person}'s authorisation for this call. It will be renewed on the "
    "next attempt; if it keeps failing they need to re-authorise from their DAIV account settings."
)
REFUSAL_REFRESH_FAILED = (
    "error: {person}'s {provider} authorisation could not be renewed just now — {provider} did not "
    "answer the renewal. The authorisation is intact; retry shortly."
)
REFUSAL_UNREADABLE = (
    "error: {person}'s stored {provider} authorisation can no longer be read by this deployment, so "
    "it has been cleared. They need to re-authorise from their DAIV account settings."
)
REFUSAL_WEBHOOK_RUNS_DISABLED = (
    "error: Cross-project access is not enabled for runs started by an issue or merge request event on this "
    "DAIV deployment. Only {attached} can be reached."
)
REFUSAL_INSUFFICIENT_SCOPE = (
    "error: {person}'s {provider} authorisation does not permit this operation on {project}. "
    "Re-authorising from DAIV account settings will request the required access."
)
REFUSAL_PLATFORM_DENIED = (
    "error: {project} is not accessible to {person} on {provider}. It may not exist, or they may not have access."
)
REFUSAL_WRONG_HOST = "error: {project} is not on {host}, which is the only platform this deployment is configured for."
REFUSAL_PROJECT_IS_A_FLAG = "error: The project must be a project path, not a flag."
REFUSAL_PROJECT_NOT_A_PATH = "error: The project must be a single project path (e.g. 'group/name')."
REFUSAL_PROJECT_NOT_OWNER_REPO = "error: The project must be a repository given as 'owner/name', with no host."
REFUSAL_DESTRUCTIVE_CROSS_PROJECT = (
    "error: '{action}' changes state beyond reading and commenting, so it is only allowed on "
    "{attached}, not on {project}. Run it against the current project, or ask someone with access "
    "to {project} to do it there."
)
REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT = (
    "error: Inline merge request diff comments can only be created on {attached}, not on {project}. "
    "Use a regular merge request note there instead."
)
REFUSAL_BODY_FILE_CROSS_PROJECT = (
    "error: Outside the current project a body must be passed as text with --body; it cannot come from a file, a "
    "template or commit messages."
)
REFUSAL_FILE_ARGUMENT_CROSS_PROJECT = (
    "error: A value starting with '@' is read from a file, which is only allowed on the current project. Pass the "
    "text itself, writing a literal leading '@' as '@@'."
)
REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT = (
    "error: '{flag}' is not a flag this tool can check outside the current project. Spell each flag out in full, "
    "as '{command} --help' lists it."
)
REFUSAL_NO_ACTING_IDENTITY = "error: Could not establish the requesting user's identity for {project}."
REFUSAL_BOT_LABEL_CROSS_PROJECT = (
    f"error: '{BOT_LABEL}', '{BOT_AUTO_LABEL}' and '{BOT_MAX_LABEL}' start a DAIV run, so they cannot be added "
    "outside {attached}. On {project} they would launch a run as the person who requested this one; leave them off, "
    "or ask someone with access to {project} to add them."
)
REFUSAL_QUICK_ACTION_CROSS_PROJECT = (
    "error: A body line starting with '/' is a GitLab quick action (/label, /close, /merge, /assign, ...), and quick "
    "actions cannot be used outside {attached}, so this is refused on {project}. Reword the line so it does not "
    "start with '/'."
)

REFUSAL_FOREIGN_REFERENCE = (
    "error: '{reference}' names a repository other than {target}, and gh would act on that repository instead. "
    "Pass the issue or pull request number, and set the `project` argument to reach the repository it belongs to."
)
REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE = (
    "error: '{reference}' names a repository other than {target}, and other repositories cannot be reached from this "
    "run. Pass the number of an issue or pull request in {target}."
)
REFUSAL_UNRECORDED_CROSS_PROJECT = (
    "error: The call against {project} ran, but its result is withheld because DAIV could not record the access, and "
    "that record is what keeps the result to the person it was fetched for. Do not repeat a write; retry a read later."
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _redact_token(text: str, token: str | None) -> str:
    """Blank the acting token out of text bound for a log record."""
    return text.replace(token, "***") if token else text


# stderr is never echoed cross-project, so failures are classified from it instead. Anything
# unmatched degrades to a generic failure rather than leaking the text.
_PLATFORM_DENIED_MARKERS = ("404", "403", "not found", "forbidden", "could not resolve", "no such")
_PLATFORM_UNAUTHORIZED_RE = re.compile(
    # A bare "401" also matches issue, run and branch numbers the CLIs echo back, so the digits
    # only count next to an HTTP-status word or at the start of a line, as both CLIs print them.
    r"\bunauthorized\b|\bbad credentials\b|\binvalid_token\b|\btoken (?:is )?(?:expired|invalid|revoked)\b"
    r"|(?:\b(?:http|status)\b\W{0,8}|^)401\b",
    re.IGNORECASE | re.MULTILINE,
)

# Both projections of a reason in one table, so the assert below covers both and a new reason
# cannot acquire an outcome while falling through to the wrong message.
_REFUSAL_BY_REASON: dict[CredentialReason, tuple[str, str]] = {
    CredentialReason.DISABLED: (REFUSAL_DISABLED, _Outcome.DENIED_DISABLED),
    CredentialReason.NO_ACTING_USER: (REFUSAL_NO_ACTING_USER, _Outcome.DENIED_NO_CREDENTIAL),
    CredentialReason.NO_CREDENTIAL: (REFUSAL_NO_CREDENTIAL, _Outcome.DENIED_NO_CREDENTIAL),
    CredentialReason.EXPIRED: (REFUSAL_EXPIRED, _Outcome.DENIED_NO_CREDENTIAL),
    CredentialReason.REVOKED: (REFUSAL_REVOKED, _Outcome.DENIED_NO_CREDENTIAL),
    CredentialReason.INSUFFICIENT_SCOPE: (REFUSAL_INSUFFICIENT_SCOPE, _Outcome.DENIED_NO_CREDENTIAL),
    CredentialReason.WEBHOOK_RUNS_DISABLED: (REFUSAL_WEBHOOK_RUNS_DISABLED, _Outcome.DENIED_DISABLED),
    CredentialReason.REFRESH_FAILED: (REFUSAL_REFRESH_FAILED, _Outcome.ERROR),
    CredentialReason.UNREADABLE: (REFUSAL_UNREADABLE, _Outcome.ERROR),
}
assert set(_REFUSAL_BY_REASON) == set(CredentialReason), "every refusal reason needs a message and an outcome"


@dataclass(frozen=True, repr=False)
class TargetDecision:
    """Where a call goes and under whose identity.

    ``refusal`` set is the whole answer: a refused cross-project call is never retried under the
    service identity. ``target`` unset with no refusal is the attached-project path, unchanged from
    before this feature existed. ``acting_user_id`` is the DAIV account whose grant the call spends.
    """

    refusal: str | None = None
    target: str | None = None
    token: str | None = None
    acting_user_id: int | None = None

    def __post_init__(self) -> None:
        # With this held, a runner reading ``self.token or <service token>`` cannot run a
        # cross-project call under DAIV's own identity.
        if self.target is not None and not self.token:
            raise ValueError("a cross-project decision must carry the acting person's token")

    @property
    def is_cross_project(self) -> bool:
        return self.target is not None

    def __repr__(self) -> str:
        # This value is a live local across the runners' ``logger.exception``, so a generated
        # repr is how a person's token would reach an error report.
        return (
            f"TargetDecision(target={self.target!r}, refused={self.refusal is not None}, "
            f"has_token={bool(self.token)}, acting_user_id={self.acting_user_id!r})"
        )


CROSS_PROJECT_DENIED_ACTIONS: dict[GitPlatform, dict[str, frozenset[str]]] = {
    GitPlatform.GITLAB: GITLAB_CROSS_PROJECT_DENIED_ACTIONS,
    GitPlatform.GITHUB: GITHUB_CROSS_PROJECT_DENIED_ACTIONS,
}

# ``update`` stays reachable cross-project, since editing a description is a write the agent exists to make,
# so the flags that turn a write into closing, locking, relocating, reassigning or repointing are refused instead.
CROSS_PROJECT_DENIED_FLAGS: dict[GitPlatform, frozenset[str]] = {
    GitPlatform.GITLAB: frozenset({
        "--state-event",
        "--to-project-id",
        "--target-branch",
        "--assignee-ids",
        "--assignee-id",
        "--discussion-locked",
    }),
    GitPlatform.GITHUB: frozenset({
        "--add-assignee",
        "--remove-assignee",
        "--milestone",
        "--remove-milestone",
        "--base",
        "--edit-last",
        "--delete-last",
    }),
}
# Turning confidentiality off on an existing issue discloses it; creating a confidential one does not.
CROSS_PROJECT_DENIED_UPDATE_FLAGS: dict[GitPlatform, frozenset[str]] = {
    GitPlatform.GITLAB: frozenset({"--confidential"}),
    GitPlatform.GITHUB: frozenset(),
}
# Read filters share these names (``project-merge-request list --target-branch``), so only writes are checked.
_FLAG_CHECKED_ACTIONS = frozenset({"create", "update", "edit", "comment"})

# A label event, unlike a body, carries no marker the webhook could read, so these writes may not name a bot label.
_LABEL_FLAGS: dict[GitPlatform, frozenset[str]] = {
    GitPlatform.GITLAB: frozenset({"--labels"}),
    GitPlatform.GITHUB: frozenset({"--label", "--add-label"}),
}
_BOT_LABELS = frozenset(label.lower() for label in (BOT_LABEL, BOT_AUTO_LABEL, BOT_MAX_LABEL))

_BODY_FLAGS: dict[GitPlatform, frozenset[str]] = {
    GitPlatform.GITLAB: frozenset({"--body", "--description", "--note"}),
    GitPlatform.GITHUB: frozenset({"--body"}),
}
# Bodies the loop marker cannot reach without rewriting what the person named, so they are refused instead.
_UNMARKABLE_BODY_FLAGS: dict[GitPlatform, frozenset[str]] = {
    GitPlatform.GITLAB: frozenset(),
    GitPlatform.GITHUB: frozenset({"--body-file", "--template", "--recover"}),
}

_GH_VIEW_FLAGS = (
    {"--comments": False, "--jq": True, "--json": True, "--template": True, "--web": False},
    {"c": "--comments", "q": "--jq", "t": "--template", "w": "--web"},
)
_GH_COMMENT_FLAGS = (
    {"--body": True, "--body-file": True, "--edit-last": False, "--editor": False, "--web": False},
    {"b": "--body", "F": "--body-file", "e": "--editor", "w": "--web"},
)
# From ``gh <cmd> --help`` (gh 2.45), for every allowed gh command that names an issue or pull request or creates one:
# ``({long flag: takes a value}, {shorthand: long flag})``.
_GITHUB_FLAGS: dict[tuple[str, str], tuple[dict[str, bool], dict[str, str]]] = {
    ("issue", "create"): (
        {
            "--assignee": True,
            "--body": True,
            "--body-file": True,
            "--label": True,
            "--milestone": True,
            "--project": True,
            "--recover": True,
            "--template": True,
            "--title": True,
            "--web": False,
        },
        {
            "a": "--assignee",
            "b": "--body",
            "F": "--body-file",
            "l": "--label",
            "m": "--milestone",
            "p": "--project",
            "T": "--template",
            "t": "--title",
            "w": "--web",
        },
    ),
    ("issue", "edit"): (
        {
            "--add-assignee": True,
            "--add-label": True,
            "--add-project": True,
            "--body": True,
            "--body-file": True,
            "--milestone": True,
            "--remove-assignee": True,
            "--remove-label": True,
            "--remove-project": True,
            "--title": True,
        },
        {"b": "--body", "F": "--body-file", "m": "--milestone", "t": "--title"},
    ),
    ("issue", "comment"): _GH_COMMENT_FLAGS,
    ("issue", "view"): _GH_VIEW_FLAGS,
    ("issue", "close"): ({"--comment": True, "--reason": True}, {"c": "--comment", "r": "--reason"}),
    ("issue", "reopen"): ({"--comment": True}, {"c": "--comment"}),
    ("issue", "lock"): ({"--reason": True}, {"r": "--reason"}),
    ("issue", "unlock"): ({}, {}),
    ("issue", "develop"): (
        {"--base": True, "--branch-repo": True, "--checkout": False, "--list": False, "--name": True},
        {"b": "--base", "c": "--checkout", "l": "--list", "n": "--name"},
    ),
    ("pr", "edit"): (
        {
            "--add-assignee": True,
            "--add-label": True,
            "--add-project": True,
            "--add-reviewer": True,
            "--base": True,
            "--body": True,
            "--body-file": True,
            "--milestone": True,
            "--remove-assignee": True,
            "--remove-label": True,
            "--remove-project": True,
            "--remove-reviewer": True,
            "--title": True,
        },
        {"B": "--base", "b": "--body", "F": "--body-file", "m": "--milestone", "t": "--title"},
    ),
    ("pr", "comment"): _GH_COMMENT_FLAGS,
    ("pr", "view"): _GH_VIEW_FLAGS,
    ("pr", "review"): (
        {"--approve": False, "--body": True, "--body-file": True, "--comment": False, "--request-changes": False},
        {"a": "--approve", "b": "--body", "F": "--body-file", "c": "--comment", "r": "--request-changes"},
    ),
    ("pr", "checks"): (
        {"--fail-fast": False, "--interval": True, "--required": False, "--watch": False, "--web": False},
        {"i": "--interval", "w": "--web"},
    ),
    ("pr", "diff"): ({"--color": True, "--name-only": False, "--patch": False, "--web": False}, {"w": "--web"}),
    ("pr", "close"): ({"--comment": True, "--delete-branch": False}, {"c": "--comment", "d": "--delete-branch"}),
    ("pr", "reopen"): ({"--comment": True}, {"c": "--comment"}),
    ("pr", "lock"): ({"--reason": True}, {"r": "--reason"}),
    ("pr", "unlock"): ({}, {}),
}
_GH_INHERITED_FLAGS = {"--help": False}
_GITHUB_ISSUE_REFERENCE = re.compile(r"(?P<repo>[^\s/#]+/[^\s/#]+)#\d+")

_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_GITLAB_PROJECT_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


@dataclass(frozen=True)
class _FlagUse:
    """One flag as the CLI will read it: its full name, the argument whose text ends with its value, and the value."""

    name: str
    value_index: int | None
    value: str | None = None


class _UncheckedFlagError(ValueError):
    """A flag whose meaning, and so whose value, cannot be told: an unknown or ambiguous option, or an unknown
    letter in a short-flag cluster."""


@functools.cache
def _gitlab_cli_parser() -> argparse.ArgumentParser:
    from gitlab import cli as gitlab_cli

    return gitlab_cli._get_parser()


@functools.cache
def _gitlab_cli_flags(resource: str, action: str) -> dict[str, bool]:
    """``{long option: takes a value}`` for one python-gitlab subcommand, read from the CLI's own parser."""
    parser = _gitlab_cli_parser()
    for name in (resource, action):
        subparsers = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        child = subparsers.choices.get(name) if subparsers is not None else None
        if child is None:
            return {}
        parser = child
    return {option: entry.nargs != 0 for entry in parser._actions for option in entry.option_strings}


def _resolve_gitlab_flag(spelled: str, flags: dict[str, bool]) -> str:
    """The option argparse reads ``spelled`` as: itself, or the one option it abbreviates."""
    if spelled in flags:
        return spelled
    matches = [flag for flag in flags if flag.startswith(spelled)]
    return matches[0] if len(matches) == 1 else spelled


def _separate_value_use(name: str, args: list[str], index: int) -> _FlagUse:
    """A flag whose value is the next argument, which may be missing."""
    return _FlagUse(name, index, args[index]) if index < len(args) else _FlagUse(name, None)


def _gitlab_flag_uses(resource: str, action: str, args: list[str]) -> list[_FlagUse]:
    # The subcommand parsers accept any unambiguous prefix of an option, so ``--state-ev`` is ``--state-event``.
    flags = _gitlab_cli_flags(resource, action)
    uses: list[_FlagUse] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--") and len(arg) > 2:
            spelled, has_value, attached = arg.partition("=")
            name = _resolve_gitlab_flag(spelled, flags)
            if name not in flags:
                raise _UncheckedFlagError(spelled)
            if has_value:
                uses.append(_FlagUse(name, index, attached))
            elif flags[name]:
                index += 1
                uses.append(_separate_value_use(name, args, index))
            else:
                uses.append(_FlagUse(name, None))
        index += 1
    return uses


def _github_args(resource: str, action: str, args: list[str], *, strict: bool) -> tuple[list[_FlagUse], list[str]]:
    """``(flags, positional arguments)`` read the way gh's flag parser does: ``--flag[=value]``, ``-f value``,
    ``-fvalue``, ``-f=value`` and clusters such as ``-wb value``.

    ``strict`` raises ``_UncheckedFlagError`` on a flag the command's ``_GITHUB_FLAGS`` entry lacks. Otherwise such a
    flag is read as taking no value, so the argument after it is still treated as positional.
    """
    long_flags, shorthands = _GITHUB_FLAGS.get((resource, action), ({}, {}))
    long_flags = long_flags | _GH_INHERITED_FLAGS
    uses: list[_FlagUse] = []
    positionals: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positionals += args[index + 1 :]
            break
        if arg.startswith("--"):
            name, has_value, attached = arg.partition("=")
            if strict and name not in long_flags:
                raise _UncheckedFlagError(name)
            if has_value:
                uses.append(_FlagUse(name, index, attached))
            elif long_flags.get(name, False):
                index += 1
                uses.append(_separate_value_use(name, args, index))
            else:
                uses.append(_FlagUse(name, None))
        elif arg.startswith("-") and len(arg) > 1:
            cluster = arg[1:]
            while cluster:
                name = shorthands.get(cluster[0])
                if name is None:
                    if strict:
                        raise _UncheckedFlagError(arg)
                    cluster = cluster[1:]
                    continue
                if len(cluster) > 2 and cluster[1] == "=":
                    uses.append(_FlagUse(name, index, cluster[2:]))
                    break
                if not long_flags[name]:
                    uses.append(_FlagUse(name, None))
                    cluster = cluster[1:]
                    continue
                if len(cluster) > 1:
                    uses.append(_FlagUse(name, index, cluster[1:]))
                else:
                    index += 1
                    uses.append(_separate_value_use(name, args, index))
                break
        else:
            positionals.append(arg)
        index += 1
    return uses, positionals


def _github_flag_uses(resource: str, action: str, args: list[str]) -> list[_FlagUse]:
    """A cross-project gh write's flags, every one of them known to ``_GITHUB_FLAGS``; reads are not checked."""
    if action not in _FLAG_CHECKED_ACTIONS:
        return []
    return _github_args(resource, action, args, strict=True)[0]


def foreign_github_reference(resource: str, action: str, args: list[str], *, target: str) -> str | None:
    """The first positional issue or pull request URL, or ``owner/repo#N``, naming a repository other than ``target``.

    gh takes the repository from such an argument rather than from ``--repo``.
    """
    if (resource, action) not in _GITHUB_FLAGS:
        return None
    host = platform_host(GitPlatform.GITHUB).lower()
    for arg in _github_args(resource, action, args, strict=False)[1]:
        if "://" in arg:
            parsed = urlparse(arg)
            repository = "/".join(parsed.path.strip("/").split("/")[:2])
            if (parsed.hostname or "").lower() != host or repository.lower() != target.lower():
                return arg
        elif (reference := _GITHUB_ISSUE_REFERENCE.fullmatch(arg)) and reference["repo"].lower() != target.lower():
            return arg
    return None


def _flag_uses(provider: GitPlatform, resource: str, action: str, args: list[str]) -> list[_FlagUse]:
    if provider == GitPlatform.GITLAB:
        return _gitlab_flag_uses(resource, action, args)
    return _github_flag_uses(resource, action, args)


def _reads_a_file(arg: str) -> bool:
    """python-gitlab replaces any value starting with ``@`` by that file's content; ``@@`` escapes it."""
    value = arg.partition("=")[2] if arg.startswith("--") else arg
    return value.startswith("@") and not value.startswith("@@")


def _names_a_bot_label(value: str) -> bool:
    """Whether a comma-separated label list names a bot label.

    Every whitespace, control, format and quote character is dropped before comparing, wherever it sits, so
    whatever the CLI (``str.strip()`` per item), the quote handling or the platform trims cannot turn a refused
    name into an accepted one.
    """
    for label in unicodedata.normalize("NFKC", value).split(","):
        core = "".join(char for char in label if char.isprintable() and not char.isspace() and char not in "\"'")
        if core.lower() in _BOT_LABELS:
            return True
    return False


def _has_quick_action_line(value: str) -> bool:
    return any(line.lstrip().startswith("/") for line in value.splitlines())


def _cross_project_policy_refusal(
    resource: str, action: str, args: list[str], *, provider: GitPlatform, attached: str, project: str
) -> str | None:
    """Refuse a destructive verb or flag, a reference to a third repository, an unknown flag, a bot label, a quick
    action or an unmarkable body, outside the attached project, before a credential is spent."""
    if action in CROSS_PROJECT_DENIED_ACTIONS.get(provider, {}).get(resource, frozenset()):
        return REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=attached, project=project
        )
    if provider == GitPlatform.GITHUB and (
        reference := foreign_github_reference(resource, action, args, target=project)
    ):
        return REFUSAL_FOREIGN_REFERENCE.format(reference=reference, target=project)
    try:
        uses = _flag_uses(provider, resource, action, args)
    except _UncheckedFlagError as exc:
        return REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(flag=exc.args[0], command=f"{resource} {action}")
    names = {use.name for use in uses}
    if resource == "project-merge-request-discussion" and action == "create" and "--position" in names:
        # The inline path goes through RepoClient, which holds the service token.
        return REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT.format(attached=attached, project=project)
    denied_flags = CROSS_PROJECT_DENIED_FLAGS[provider]
    if action == "update":
        denied_flags |= CROSS_PROJECT_DENIED_UPDATE_FLAGS[provider]
    denied = next((use.name for use in uses if use.name in denied_flags), None)
    if denied is not None and action in _FLAG_CHECKED_ACTIONS:
        return REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action} {denied}", attached=attached, project=project
        )
    if action in _FLAG_CHECKED_ACTIONS and any(
        use.name in _LABEL_FLAGS[provider] and _names_a_bot_label(use.value or "") for use in uses
    ):
        return REFUSAL_BOT_LABEL_CROSS_PROJECT.format(attached=attached, project=project)
    # GitLab runs these lines as the person, past every denied verb and flag above.
    if (
        provider == GitPlatform.GITLAB
        and action in _FLAG_CHECKED_ACTIONS
        and any(use.name in _BODY_FLAGS[provider] and _has_quick_action_line(use.value or "") for use in uses)
    ):
        return REFUSAL_QUICK_ACTION_CROSS_PROJECT.format(attached=attached, project=project)
    if names & _UNMARKABLE_BODY_FLAGS[provider]:
        return REFUSAL_BODY_FILE_CROSS_PROJECT
    if provider == GitPlatform.GITLAB and any(_reads_a_file(arg) for arg in args):
        return REFUSAL_FILE_ARGUMENT_CROSS_PROJECT
    return None


def _validate_project(project: str, attached: str, provider: GitPlatform) -> tuple[str | None, str | None]:
    """``(target, refusal)``. Both ``None`` means the attached-project path.

    ``argv`` reaches ``create_subprocess_exec`` without a shell, so the risk here is flag
    confusion, not quoting. gh reads ``-R HOST/OWNER/REPO``, so a GitHub target is held to
    ``owner/name``: anything longer would send the person's token to another host.
    """
    candidate = project.strip()
    if not candidate:
        return None, None
    if candidate.startswith("-"):
        return None, REFUSAL_PROJECT_IS_A_FLAG
    if _CONTROL_CHARACTERS.search(candidate) or any(character.isspace() for character in candidate):
        return None, REFUSAL_PROJECT_NOT_A_PATH
    if "://" in candidate:
        host = platform_host(provider)
        parsed = urlparse(candidate)
        if parsed.hostname != host:
            return None, REFUSAL_WRONG_HOST.format(project=candidate, host=host)
        candidate = parsed.path.strip("/")
        if not candidate:
            return None, REFUSAL_PROJECT_NOT_A_PATH
    if candidate == attached:
        return None, None
    shape = _GITHUB_REPOSITORY if provider == GitPlatform.GITHUB else _GITLAB_PROJECT_PATH
    if not shape.fullmatch(candidate) or any(segment in {".", ".."} for segment in candidate.split("/")):
        return None, REFUSAL_PROJECT_NOT_OWNER_REPO if provider == GitPlatform.GITHUB else REFUSAL_PROJECT_NOT_A_PATH
    return candidate, None


def mark_cross_project_body(args: list[str], *, provider: GitPlatform) -> list[str]:
    """Append the loop marker to every body published outside the attached project.

    Cross-project writes are attributed to a person, not to the bot, so the webhook's
    ``current_user.id`` check cannot recognise them as DAIV's own. Without the marker, a comment
    DAIV leaves in a DAIV-watched project starts a new run on itself. Every occurrence is marked,
    because both CLIs keep the last value of a repeated flag.
    """
    resource, action, rest = args[0], args[1], args[2:]
    marked = list(args)
    if action not in _FLAG_CHECKED_ACTIONS:
        return marked
    body_indexes = {
        use.value_index + 2
        for use in _flag_uses(provider, resource, action, rest)
        if use.name in _BODY_FLAGS[provider] and use.value_index is not None
    }
    for index in body_indexes:
        marked[index] = f"{marked[index]}\n\n{CROSS_PROJECT_CONTENT_MARKER}"
    if not body_indexes:
        # An unmarked cross-project write in a DAIV-watched project is what starts a run on DAIV's own output.
        logger.warning("[git-platform] Cross-project %s %s published no recognised body flag.", resource, action)
    return marked


async def _acting_person_label(acting_user_id: int | None) -> str:
    """How a refusal names the person. Falls back to a role, never to an id."""
    if not acting_user_id:
        return "The requesting user"
    from accounts.models import User

    user = await User.objects.filter(pk=acting_user_id).afirst()
    return str(user) if user is not None else "The requesting user"


def _credential_refusal(
    reason: CredentialReason | None, *, attached: str, project: str, provider: GitPlatform, person: str
) -> tuple[str, str]:
    """``(refusal the agent reads, audit outcome)`` for a reason no token could be issued for."""
    template, outcome = _REFUSAL_BY_REASON.get(reason, _REFUSAL_BY_REASON[CredentialReason.NO_CREDENTIAL])
    # Every template draws from this one kwargs set; str.format ignores the keys it does not name.
    return template.format(attached=attached, project=project, provider=provider.value, person=person), outcome


async def _record_cross_project_access(
    runtime: ToolRuntime[RuntimeCtx],
    *,
    provider: GitPlatform,
    target_repo_id: str,
    outcome: str,
    acting_user_id: int | None,
    person: str | None = None,
) -> bool:
    """One row per cross-project attempt, allowed or refused; ``False`` when it could not be written.

    A refused call must still be recorded — a pattern of refusals is what an auditor is looking
    for — but losing the row must never cost the run its answer. An allowed call's row is different:
    writing it is what keeps the result to the person it was fetched for (``Session.cross_project_user_ids``).
    """
    from codebase.models import CrossProjectAccessRecord

    try:
        await CrossProjectAccessRecord.objects.acreate(
            thread_id=str(runtime.config.get("configurable", {}).get("thread_id", "") or ""),
            acting_user_id=acting_user_id,
            acting_user_label=(person or await _acting_person_label(acting_user_id))[:255],
            provider=provider.value,
            target_repo_id=target_repo_id[:255],
            outcome=outcome,
        )
    except Exception:
        logger.exception("[git-platform] Failed to record cross-project access to %s", target_repo_id)
        return False
    return True


class _CredentialOwner(TypedDict, total=False):
    acting_user_id: int
    platform_uid: str


def _credential_owner(ctx: RuntimeCtx) -> _CredentialOwner:
    """Whose grant a cross-project call may spend: a webhook's platform uid, else a DAIV sign-in, else nobody."""
    if ctx.acting_platform_uid is not None:
        return {"platform_uid": ctx.acting_platform_uid}
    if ctx.acting_user_id is not None and ctx.acting_user_authenticated:
        return {"acting_user_id": ctx.acting_user_id}
    return {}


def _signed_in_user_id(ctx: RuntimeCtx) -> int | None:
    """The sign-in an attempt is attributed to: only when it is the identity ``_credential_owner`` spends."""
    if ctx.acting_platform_uid is not None or not ctx.acting_user_authenticated:
        return None
    return ctx.acting_user_id


async def record_refused_before_decision(
    runtime: ToolRuntime[RuntimeCtx], *, provider: GitPlatform, project: str
) -> None:
    """Audit a cross-project attempt the allow-list refused before ``decide_target`` ran.

    The allow-list check has to come first (it is what keeps an unknown subcommand from reaching
    the target logic at all), so its refusals would otherwise be the one cross-project attempt
    with no row.
    """
    target, _ = _validate_project(project, runtime.context.repository.slug, provider)
    if target is not None:
        await _record_cross_project_access(
            runtime,
            provider=provider,
            target_repo_id=target,
            outcome=_Outcome.DENIED_POLICY,
            acting_user_id=_signed_in_user_id(runtime.context),
        )


async def decide_target(
    resource: str,
    action: str,
    args: list[str],
    project: str,
    runtime: ToolRuntime[RuntimeCtx],
    *,
    provider: GitPlatform,
    cross_project_enabled: bool,
) -> TargetDecision:
    """Pick the project and the identity for one call, and record the attempt when it crosses out.

    Every pre-flight refusal — bad path, capability off, denied verb or flag, body from a file,
    unusable credential — is decided and audited here, so a new caller cannot inherit the audit
    row without the policy gate. Execution-time failures are classified and audited by
    ``_cross_project_failure``; between them, no refusal path is the runner's to remember.
    """
    attached = runtime.context.repository.slug
    target, refusal = _validate_project(project, attached, provider)

    if refusal is not None:
        logger.warning(
            "[git-platform] Rejected %s project argument %r for thread=%s",
            provider.value,
            project.strip()[:255],
            runtime.config.get("configurable", {}).get("thread_id", ""),
        )
        await _record_cross_project_access(
            runtime,
            provider=provider,
            target_repo_id=project.strip(),
            outcome=_Outcome.ERROR,
            acting_user_id=_signed_in_user_id(runtime.context),
        )
        return TargetDecision(refusal=refusal)

    if target is None:
        return TargetDecision()

    signed_in_user_id = _signed_in_user_id(runtime.context)

    if not cross_project_enabled:
        logger.warning(
            "[git-platform] Refused cross-project %s access to %s: the capability is disabled.", provider.value, target
        )
        await _record_cross_project_access(
            runtime,
            provider=provider,
            target_repo_id=target,
            outcome=_Outcome.DENIED_DISABLED,
            acting_user_id=signed_in_user_id,
        )
        return TargetDecision(refusal=REFUSAL_DISABLED.format(attached=attached))

    # Before ``aresolve_access_token``: resolving first would spend a GitLab refresh-token
    # rotation on a call that can never run.
    if policy_refusal := _cross_project_policy_refusal(
        resource, action, args, provider=provider, attached=attached, project=target
    ):
        logger.warning(
            "[git-platform] Refused cross-project %s '%s %s' against %s by policy.",
            provider.value,
            resource,
            action,
            target,
        )
        await _record_cross_project_access(
            runtime,
            provider=provider,
            target_repo_id=target,
            outcome=_Outcome.DENIED_POLICY,
            acting_user_id=signed_in_user_id,
        )
        return TargetDecision(refusal=policy_refusal)

    resolved = await aresolve_access_token(provider=provider, **_credential_owner(runtime.context))
    # The resolver names no owner when it found no grant; a sign-in it searched by still knows whose attempt it was.
    acting_user_id = resolved.user_id if resolved.user_id is not None else signed_in_user_id
    if resolved.ok:
        if not resolved.token:
            # A branch rather than an ``or``: substituting the service token here would run the
            # call against another project under DAIV's own identity.
            logger.error(
                "[git-platform] Cross-project %s decision for %s carried no token; refusing rather "
                "than falling back to the service identity.",
                provider.value,
                target,
            )
            await _record_cross_project_access(
                runtime, provider=provider, target_repo_id=target, outcome=_Outcome.ERROR, acting_user_id=acting_user_id
            )
            return TargetDecision(refusal=REFUSAL_NO_ACTING_IDENTITY.format(project=target))
        return TargetDecision(target=target, token=resolved.token, acting_user_id=acting_user_id)

    person = await _acting_person_label(acting_user_id)
    credential_refusal, outcome = _credential_refusal(
        resolved.reason, attached=attached, project=target, provider=provider, person=person
    )
    # Logged at ``error`` for the two reasons that mean the deployment cannot renew anyone's grant;
    # a missing or revoked grant is the person's own to fix.
    log = logger.error if outcome == _Outcome.ERROR else logger.warning
    log(
        "[git-platform] No usable %s credential for user_id=%s targeting %s (reason=%s).",
        provider.value,
        acting_user_id,
        target,
        resolved.reason,
    )
    await _record_cross_project_access(
        runtime, provider=provider, target_repo_id=target, outcome=outcome, acting_user_id=acting_user_id, person=person
    )
    return TargetDecision(refusal=credential_refusal)


async def _cross_project_failure(
    stderr_text: str,
    runtime: ToolRuntime[RuntimeCtx],
    *,
    project: str,
    provider: GitPlatform,
    acting_user_id: int | None,
    token: str | None = None,
    returncode: int | None = None,
) -> str:
    """The tool result for a failed cross-project call, audited on the way out.

    stderr is classified, never echoed: it can carry token fragments, and repository names the
    person is not entitled to see. The denied string deliberately does not distinguish "does not
    exist" from "you may not see it", mirroring the platform, so the tool cannot be used as an
    existence oracle for private projects.
    """
    lowered = stderr_text.lower()
    person = await _acting_person_label(acting_user_id)

    async def refuse(message: str, outcome: str) -> str:
        await _record_cross_project_access(
            runtime,
            provider=provider,
            target_repo_id=project,
            outcome=outcome,
            acting_user_id=acting_user_id,
            person=person,
        )
        return message

    if _PLATFORM_UNAUTHORIZED_RE.search(stderr_text):
        # Drop the cached token but leave the stored grant: only the refresh endpoint can say a grant
        # is dead, and a misread of CLI prose must not cost the person a re-authorisation.
        if acting_user_id:
            try:
                await ainvalidate_cached_token(user_id=acting_user_id, provider=provider)
            except Exception as exc:
                # No exc_info: Sentry would attach this frame's locals, and stderr can echo the token.
                logger.error(
                    "[git-platform] Failed to drop the cached %s token (%s)", provider.value, type(exc).__name__
                )
        logger.warning(
            "[git-platform] %s rejected the credential for user_id=%s against %s (exit %s).",
            provider.value,
            acting_user_id,
            project,
            returncode,
        )
        return await refuse(
            REFUSAL_CREDENTIAL_REJECTED.format(person=person, provider=provider.value), _Outcome.DENIED_NO_ACCESS
        )

    if any(marker in lowered for marker in _PLATFORM_DENIED_MARKERS):
        logger.warning(
            "[git-platform] %s denied user_id=%s access to %s (exit %s).",
            provider.value,
            acting_user_id,
            project,
            returncode,
        )
        return await refuse(
            REFUSAL_PLATFORM_DENIED.format(project=project, person=person, provider=provider.value),
            _Outcome.DENIED_NO_ACCESS,
        )

    # An unmatched stderr is the only diagnostic there is, and it matched none of the token-shaped
    # markers. Logged at error: it is as likely a platform outage as a permission denial.
    logger.error(
        "[git-platform] Unclassified cross-project %s failure against %s (exit %s): %s",
        provider.value,
        project,
        returncode,
        _redact_token(stderr_text, token),
    )
    return await refuse(
        f"error: The {provider.value} command against {project} failed. The details are withheld "
        f"because they come from a project outside this run. Re-check the subcommand, or run it "
        f"against the current project instead.",
        _Outcome.ERROR,
    )


@dataclass(frozen=True)
class CrossProjectOutcomes:
    """What a cross-project CLI call reports back (a ``CliOutcomes``): every outcome leaves an audit row, no CLI
    error text is echoed, and a result whose ALLOWED row could not be written is withheld."""

    runtime: ToolRuntime[RuntimeCtx]
    provider: GitPlatform
    display_name: str
    decision: TargetDecision

    async def _record(self, outcome: str) -> bool:
        return await _record_cross_project_access(
            self.runtime,
            provider=self.provider,
            target_repo_id=self._target,
            outcome=outcome,
            acting_user_id=self.decision.acting_user_id,
        )

    @property
    def _target(self) -> str:
        assert self.decision.target is not None, "only a cross-project decision has outcomes to report"
        return self.decision.target

    async def on_timeout(self, message: str) -> str:
        # The subprocess held the person's token and may well have completed the write before
        # the timeout; a row has to exist for the attempt either way.
        await self._record(_Outcome.ERROR)
        return message

    async def on_launch_error(self) -> str:
        await self._record(_Outcome.ERROR)
        # An exception raised around a subprocess carrying a person's token is not a safe string to hand back.
        return f"error: Failed to execute the {self.display_name} command against {self._target}."

    async def on_failure(self, returncode: int, stderr: str) -> str:
        return await _cross_project_failure(
            stderr,
            self.runtime,
            project=self._target,
            provider=self.provider,
            acting_user_id=self.decision.acting_user_id,
            token=self.decision.token,
            returncode=returncode,
        )

    async def on_success(self) -> str | None:
        if await self._record(_Outcome.ALLOWED):
            return None
        return REFUSAL_UNRECORDED_CROSS_PROJECT.format(project=self._target)
