from __future__ import annotations

import json
import logging
from datetime import date
from operator import attrgetter
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from langchain_core.messages import messages_to_dict

from sessions.archive import decode_values
from sessions.models import Session

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = "Write each archived session's contexts and runs to <output>/<thread_id>.json."

    def add_arguments(self, parser):
        parser.add_argument("output", type=Path, help="Directory to write the files to; created if missing.")
        parser.add_argument(
            "--since", type=date.fromisoformat, help="Only sessions active on or after this date (YYYY-MM-DD)."
        )
        parser.add_argument("--repo", help="Only sessions on this repository, e.g. group/project.")

    def handle(self, *args, output: Path, since: date | None, repo: str | None, **options):
        sessions = Session.objects.filter(archives__isnull=False).distinct().prefetch_related("archives", "runs")
        if since is not None:
            sessions = sessions.filter(last_active_at__date__gte=since)
        if repo:
            sessions = sessions.filter(repo_id=repo)
        output.mkdir(parents=True, exist_ok=True)
        exported, failed = 0, []
        for session in sessions.iterator(chunk_size=50):
            try:
                payload = json.dumps(_export(session), indent=2, default=str)
            except Exception:
                logger.exception("Failed to export session %s", session.thread_id)
                failed.append(session.thread_id)
                continue
            (output / f"{session.thread_id}.json").write_text(payload)
            exported += 1
        self.stdout.write(f"Exported {exported} session(s) to {output}.")
        if failed:
            raise CommandError(f"Failed to export {len(failed)} session(s), see the log: " + ", ".join(failed))


def _export(session: Session) -> dict[str, Any]:
    return {
        "thread_id": session.thread_id,
        "repo_id": session.repo_id,
        "origin": session.origin,
        "title": session.title,
        "created_at": session.created_at,
        "contexts": [
            {
                "epoch": archive.epoch,
                "updated_at": archive.updated_at,
                "closed_at": archive.closed_at,
                "messages": messages_to_dict(decode_values(archive.state_type, archive.state).get("messages") or []),
            }
            for archive in sorted(session.archives.all(), key=attrgetter("epoch"))  # ty: ignore[unresolved-attribute]
        ],
        "runs": [
            {
                "id": run.id,
                "trigger_type": run.trigger_type,
                "status": run.status,
                "agent_model": run.agent_model,
                "prompt": run.prompt,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "cost_usd": run.cost_usd,
                "created_at": run.created_at,
                "finished_at": run.finished_at,
            }
            for run in sorted(session.runs.all(), key=attrgetter("created_at"))  # ty: ignore[unresolved-attribute]
        ],
    }
