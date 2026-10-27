"""Run the ``/code-review`` skill on ReviewBench pull requests and write its findings in the judging input format.

Each pull request's base and head are fetched by sha from its ``review-bench/<owner>_<repo>`` mirror, the copy the
judge reads, shallow down to their merge base, and the review runs on a detached head checkout in a git-capable sandbox
with its network off, no MCP servers and no web tools: the upstream pull request's review comments are a golden-set
source, so a reviewer that can reach GitHub can read the answers. Findings land in
``<output-dir>/findings/<pr key>.json``, the raw reports in ``reports/``, the agent's messages, detector reports
included, in ``transcripts/`` and the model usage per agent, the review agent and each code-review detector, in
``usage/``; a pull request whose findings file exists is skipped, so a rerun resumes. Score them from a ReviewBench
checkout with ``npm run judge -- --candidate <output-dir>/findings``.
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
import threading
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import django
from django.apps import apps

from git import GitCommandError, Repo

if not apps.ready:
    django.setup()

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.messages import AIMessage, messages_to_dict  # noqa: E402
from langchain_core.tracers.context import register_configure_hook  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from sandbox_envs.services import build_sandbox_spec  # noqa: E402

from automation.agent.agent_settings import RunOverrides, resolve_agent_settings  # noqa: E402
from automation.agent.context import set_runtime_ctx  # noqa: E402
from automation.agent.graph import create_daiv_agent  # noqa: E402
from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH  # noqa: E402
from automation.agent.usage_tracking import (  # noqa: E402
    CostAwareUsageMetadataCallbackHandler,
    build_usage_summary,
    track_usage_metadata,
)
from automation.agent.utils import build_langsmith_config  # noqa: E402
from automation.agent.validators import validate_agent_override  # noqa: E402
from automation.agent.workspace.sandbox import SandboxWorkspace  # noqa: E402
from automation.agent.workspace.session import SandboxSession, acquire_sandbox  # noqa: E402
from codebase.base import Scope  # noqa: E402
from codebase.clients.utils import safe_slug  # noqa: E402
from core.site_settings import site_settings  # noqa: E402
from evals.swe_client import SWERepoClient  # noqa: E402
from tests.integration_tests.code_review_grading import is_degraded, is_review_report, parse_report  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping
    from uuid import UUID

    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult

    from automation.agent.usage_tracking import UsageSummary
    from codebase.base import Repository
    from tests.integration_tests.code_review_grading import Finding

AGENT = "daiv"
MIRROR_ORG = "review-bench"
# The seeded GLOBAL default, python:3.12-alpine, has no git, so the review could not diff base and head.
SANDBOX_IMAGE = "ghcr.io/astral-sh/uv:python3.14-bookworm"
SANDBOX_REPO_ROOT = "/workspace/repo/"
DEFAULT_MODEL = "openrouter:z-ai/glm-5.2"
REPORTED_SEVERITIES = frozenset({"Critical", "Important", "Suggestion"})
CORPUS_FILES = {"test": "corpus/test/test.json", "full": "corpus/manifest.json"}
FETCH_DEPTH = 64
MAX_DEEPENINGS = 6
REVIEW_AGENT = "review"

_URL = re.compile(r"https?://\S+")
_PATH_LINES = re.compile(r"(?P<path>[\w./@+\[\]-]*[\w@+\]-]):(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?")
_BLOB_LINES = re.compile(r"/blob/[0-9a-f]{7,40}/(?P<path>[^\s)#?]+)#L(?P<start>\d+)(?:-L(?P<end>\d+))?")
# ``parse_report`` gives the last finding every line up to the report's end, closing prose included.
_THEMATIC_BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_BOLD_SECTION = re.compile(r"^\*\*((?:Critical|Important|Suggestion|Question|Recommended)[^*\n]*)\*\*$", re.MULTILINE)


@dataclass(frozen=True)
class Location:
    file: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Review:
    report: str
    head_files: frozenset[str]
    usage: dict[str, float]
    transcript: list[dict] = field(default_factory=list)
    usage_by_agent: dict[str, dict[str, float]] = field(default_factory=dict)


class AgentUsageHandler(BaseCallbackHandler):
    """Model usage per agent: a call is a detector's when its charter opens the system prompt, else the reviewer's."""

    def __init__(self, openings: Mapping[str, str]) -> None:
        self.openings = openings
        self.by_agent: dict[str, CostAwareUsageMetadataCallbackHandler] = defaultdict(
            CostAwareUsageMetadataCallbackHandler
        )
        self._agent_by_run: dict[UUID, str] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(
        self, serialized: dict, messages: list[list[BaseMessage]], *, run_id: UUID, **kwargs: object
    ) -> None:
        system = next((message.text for message in messages[0] if message.type == "system"), "") if messages else ""
        agent = next((name for name, opening in self.openings.items() if opening in system), REVIEW_AGENT)
        with self._lock:
            self._agent_by_run[run_id] = agent

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: object) -> None:
        with self._lock:
            handler = self.by_agent[self._agent_by_run.pop(run_id, REVIEW_AGENT)]
        handler.on_llm_end(response, run_id=run_id, **kwargs)


_agent_usage_var: ContextVar[AgentUsageHandler | None] = ContextVar("reviewbench_agent_usage", default=None)
register_configure_hook(_agent_usage_var, inheritable=True)


@contextmanager
def track_agent_usage() -> Generator[AgentUsageHandler]:
    handler = AgentUsageHandler(detector_openings())
    token = _agent_usage_var.set(handler)
    try:
        yield handler
    finally:
        _agent_usage_var.reset(token)


def detector_openings() -> dict[str, str]:
    """Each code-review detector's charter opening line, which starts the system prompt of the detector's calls."""
    return {
        charter.stem: next(line for line in charter.read_text().split("---", 2)[2].splitlines() if line.strip())
        for charter in sorted(CODE_REVIEW_AGENTS_PATH.glob("cr-*.md"))
    }


class ReviewBenchRepoClient(SWERepoClient):
    """A pull request's checkout as the benchmark mounts it: base and head fetched by sha, head detached, no refs.

    The repository keeps its upstream name, so the agent and its blob links see ``owner/repo``, never the mirror.
    """

    def __init__(self, mirror_url: str, base: str):
        super().__init__(repo_host="github.com")
        self.mirror_url = mirror_url
        self.base = base

    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Generator[Repo]:
        with tempfile.TemporaryDirectory(prefix=f"{safe_slug(repository.slug)}-") as tmpdir:
            repo = Repo.init(Path(tmpdir) / "repo")
            with repo.git.custom_environment(GIT_LFS_SKIP_SMUDGE="1"):
                repo.git.fetch("--quiet", f"--depth={FETCH_DEPTH}", self.mirror_url, self.base, sha)
                self._deepen_to_merge_base(repo, sha)
                repo.git.checkout("--quiet", "--detach", sha)
            (Path(repo.git_dir) / "FETCH_HEAD").unlink()
            self._loaded_repo = repo
            try:
                yield repo
            finally:
                self._loaded_repo = None

    def _deepen_to_merge_base(self, repo: Repo, head: str) -> None:
        """Deepen the shallow fetch until ``base...head`` has its merge base, unshallowing as a last resort."""
        for attempt in range(MAX_DEEPENINGS):
            try:
                repo.git.merge_base(self.base, head)
            except GitCommandError:
                repo.git.fetch("--quiet", f"--deepen={FETCH_DEPTH * 2**attempt}", self.mirror_url, self.base, head)
            else:
                return
        repo.git.fetch("--quiet", "--unshallow", self.mirror_url, self.base, head)


def mirror_url(nwo: str) -> str:
    return f"https://github.com/{MIRROR_ORG}/{nwo.replace('/', '_')}.git"


def pr_key(entry: dict) -> str:
    """``try-agent.sh``'s name for a pull request's findings file."""
    return f"{entry['nwo'].replace('/', '_')}_{entry['pr_number']}-{entry['head'][:8]}"


def load_corpus(reviewbench_dir: Path, set_name: str, indices: Iterable[int] | None = None) -> list[dict]:
    entries = json.loads((reviewbench_dir / CORPUS_FILES[set_name]).read_text())
    return entries if indices is None else [entries[index] for index in indices]


def review_request(entry: dict) -> str:
    base, head = entry["base"], entry["head"]
    request = (
        f'/code-review the pull request "{entry["title"]}": the change from base {base} to head {head}, '
        f"that is `git diff {base}...{head}`. The checkout is at the head commit."
    )
    if body := (entry.get("body") or "").strip():
        request += f"\n\n## Pull request description\n\n{body}"
    return request


def parse_locations(text: str, head_files: frozenset[str]) -> list[Location]:
    """Each tracked head-side file ``text`` names a line of, at its first named line; deleted lines are left out."""
    plain = _URL.sub(" ", text)
    named = [
        (match["path"], int(match["start"]), int(match["end"] or match["start"]))
        for match in _PATH_LINES.finditer(plain)
        if "(deleted)" not in plain[match.end() : match.end() + 16]
    ]
    named += [
        (match["path"], int(match["start"]), int(match["end"] or match["start"]))
        for match in _BLOB_LINES.finditer(text)
    ]
    locations: dict[str, Location] = {}
    for path, start, end in named:
        file = _tracked_file(path, head_files)
        if file is not None and file not in locations:
            locations[file] = Location(file=file, start_line=max(1, min(start, end)), end_line=max(1, start, end))
    return list(locations.values())


def _names_a_line(text: str) -> bool:
    return bool(_PATH_LINES.search(_URL.sub(" ", text)) or _BLOB_LINES.search(text))


def _tracked_file(path: str, head_files: frozenset[str]) -> str | None:
    path = path.removeprefix(SANDBOX_REPO_ROOT).removeprefix("./").lstrip("/")
    if path in head_files:
        return path
    candidates = [file for file in head_files if file.endswith(f"/{path}")]
    return candidates[0] if len(candidates) == 1 else None


def parse_review(report: str) -> list[Finding]:
    """``parse_report`` for a report whose section headings may be bold lines rather than markdown headings."""
    return parse_report(_BOLD_SECTION.sub(r"### \1", report))


def report_findings(findings: Iterable[Finding], head_files: frozenset[str]) -> tuple[list[dict], list[Finding]]:
    """The judging-format findings of a report, and the reported findings no head-side location could be read from.

    A finding whose location line names several files becomes one finding per file, since the judge matches within a
    file; one whose location line names no line falls back to the first line its title or details name.
    """
    located: list[dict] = []
    unlocated: list[Finding] = []
    for finding in findings:
        if finding.severity not in REPORTED_SEVERITIES:
            continue
        if _names_a_line(finding.location):
            locations = parse_locations(finding.location, head_files)
        else:
            locations = parse_locations(f"{finding.title}\n{finding.details}", head_files)[:1]
        if not locations:
            unlocated.append(finding)
            continue
        message = f"{finding.title}\n\n{_THEMATIC_BREAK.split(finding.details, maxsplit=1)[0]}".strip()
        located.extend(
            {
                "file": location.file,
                "start_line": location.start_line,
                "end_line": location.end_line,
                "message": message,
                "producer": AGENT,
            }
            for location in locations
        )
    return located, unlocated


def findings_payload(entry: dict, model_name: str, findings: list[dict], usage: dict[str, float]) -> dict:
    return {
        "pr": {
            "repo": entry["repo"],
            "pr_number": int(entry["pr_number"]),
            "base": entry["base"],
            "head": entry["head"],
        },
        "agent": f"{AGENT}/{model_name}",
        "findings": findings,
        "usage": usage,
    }


async def review(entry: dict, model_name: str, thinking_level: str | None) -> Review:
    spec = replace(await build_sandbox_spec(None), base_image=SANDBOX_IMAGE, egress=None)
    client = ReviewBenchRepoClient(mirror_url(entry["nwo"]), entry["base"])
    started = monotonic()
    async with set_runtime_ctx(
        repo_id=entry["nwo"], scope=Scope.GLOBAL, ref=entry["head"], offline=True, sandbox_spec=spec, repo_client=client
    ) as ctx:
        head_files = frozenset(ctx.gitrepo.git.ls_files("-z").split("\0")) - {""}
        session = SandboxSession(ctx.sandbox_client, ctx.sandbox, credential_source=ctx.credential_source)
        try:
            await acquire_sandbox(session, ctx, {})
            site = site_settings.snapshot()
            run = RunOverrides(
                model_names=(model_name,),
                agent_thinking_level=thinking_level or site.agent_thinking_level,
                web_search_enabled=False,
                web_fetch_enabled=False,
            )
            settings = resolve_agent_settings(site=site, repo=ctx.config, run=run)
            agent = await create_daiv_agent(
                settings=settings,
                ctx=ctx,
                workspace=SandboxWorkspace(session),
                auto_commit_changes=False,
                checkpointer=InMemorySaver(),
                ask_user_enabled=False,
            )
            config = build_langsmith_config(
                ctx,
                trigger="eval",
                model=model_name,
                thinking_level=settings.agent.thinking_level,
                agent_name=agent.get_name(),
                extra_metadata={"reviewbench_pr": pr_key(entry)},
                configurable={"thread_id": str(uuid.uuid4())},
            )
            with track_usage_metadata() as handler, track_agent_usage() as agent_usage:
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": review_request(entry)}]}, context=ctx, config=config
                )
        finally:
            await session.release(resumable=False)
    messages = result["messages"]
    report = messages[-1].text if messages and isinstance(messages[-1], AIMessage) else ""
    return Review(
        report=report,
        head_files=head_files,
        usage=_usage(build_usage_summary(handler), monotonic() - started),
        transcript=messages_to_dict(messages),
        usage_by_agent={
            agent: _usage(build_usage_summary(agent_handler)) for agent, agent_handler in agent_usage.by_agent.items()
        },
    )


def _usage(summary: UsageSummary, elapsed_seconds: float | None = None) -> dict[str, float]:
    usage: dict[str, float] = {
        "prompt_tokens": summary.input_tokens,
        "completion_tokens": summary.output_tokens,
        "total_tokens": summary.total_tokens,
    }
    if elapsed_seconds is not None:
        usage["time_in_ms"] = round(elapsed_seconds * 1000)
    if summary.cost_usd is not None:
        usage["cost_usd"] = float(Decimal(summary.cost_usd))
    return usage


async def main(
    reviewbench_dir: Path,
    set_name: str,
    output_dir: Path,
    model_name: str,
    thinking_level: str | None = None,
    pr_indices: list[int] | None = None,
) -> None:
    validate_agent_override(model_name, thinking_level)
    if site_settings.sandbox_api_key is None:
        raise SystemExit("The review needs the sandbox: configure its API key in the site settings first.")

    findings_dir, reports_dir, transcripts_dir, usage_dir = (
        output_dir / name for name in ("findings", "reports", "transcripts", "usage")
    )
    for directory in (findings_dir, reports_dir, transcripts_dir, usage_dir):
        directory.mkdir(parents=True, exist_ok=True)
    entries = load_corpus(reviewbench_dir, set_name, pr_indices)
    failed: list[str] = []

    for entry in entries:
        key = pr_key(entry)
        findings_path = findings_dir / f"{key}.json"
        if findings_path.exists():
            print(f"[{key}] already reviewed, skipping", file=sys.stderr)  # noqa: T201
            continue
        print(f"[{key}] reviewing {entry['nwo']}#{entry['pr_number']}", file=sys.stderr)  # noqa: T201
        try:
            outcome = await review(entry, model_name, thinking_level)
        except asyncio.CancelledError:
            print(f"[{key}] interrupted", file=sys.stderr)  # noqa: T201
            raise
        except Exception:
            failed.append(key)
            print(f"[{key}] review failed:", file=sys.stderr)  # noqa: T201
            traceback.print_exc()
            continue

        (reports_dir / f"{key}.md").write_text(outcome.report)
        (transcripts_dir / f"{key}.json").write_text(json.dumps(outcome.transcript, indent=1, default=str))
        (usage_dir / f"{key}.json").write_text(json.dumps(outcome.usage_by_agent, indent=2, sort_keys=True))
        if not is_review_report(outcome.report):
            failed.append(key)
            print(f"[{key}] the final message is not a review report", file=sys.stderr)  # noqa: T201
            continue
        findings, unlocated = report_findings(parse_review(outcome.report), outcome.head_files)
        findings_path.write_text(json.dumps(findings_payload(entry, model_name, findings, outcome.usage), indent=2))
        print(  # noqa: T201
            f"[{key}] {len(findings)} finding(s), {len(unlocated)} without a head-side location"
            f"{', DEGRADED' if is_degraded(outcome.report) else ''}",
            file=sys.stderr,
        )

    print(f"reviewed {len(entries) - len(failed)}/{len(entries)}; findings in {findings_dir}", file=sys.stderr)  # noqa: T201
    if failed:
        raise SystemExit(f"{len(failed)} pull request(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reviewbench-dir", type=Path, required=True, help="A checkout of review-bench/ReviewBench.")
    parser.add_argument("--set", dest="set_name", choices=sorted(CORPUS_FILES), default="test")
    parser.add_argument(
        "--pr", dest="pr_indices", type=int, nargs="+", help="Corpus indices to review; all if omitted."
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="A fresh directory per run.")
    parser.add_argument("--model-name", default=DEFAULT_MODEL)
    parser.add_argument("--thinking-level", help="The site's agent thinking level if omitted.")

    asyncio.run(main(**vars(parser.parse_args())))
