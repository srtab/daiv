import uuid
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from sessions.executor.lock import wait_unless_sessionless
from sessions.models import Session

from codebase.base import GitPlatform
from codebase.clients import RepoClient

if TYPE_CHECKING:
    from sessions.executor.lock import LockPolicy
    from sessions.executor.spec import RunOutcome

    from codebase.base import Discussion, Note


class BaseManager:
    """
    Base class for the webhook managers: each builds one agent run's ``RunSpec`` and posts its outcome back on
    the issue or merge request.
    """

    _unable_note_posted: bool = False
    """ Backing flag for :meth:`_claim_unable_note`; see that method for the rationale. """

    def __init__(
        self, *, repo_id: str, thread_id: str, mention_comment_id: str | None = None, mention_note_id: int | None = None
    ):
        self.repo_id = repo_id
        self.thread_id = thread_id
        self.mention_comment_id = mention_comment_id
        self.mention_note_id = mention_note_id
        self.client = RepoClient.create_instance()

    @property
    def reply_to_id(self) -> str | None:
        """The mention a failure comment answers under; GitHub can't reply to a comment, so only GitLab gets one."""
        return self.mention_comment_id if self.client.git_platform == GitPlatform.GITLAB else None

    def _mention_note(self, discussion: Discussion, *, fallback_index: int) -> Note:
        """The note that mentioned DAIV within the ``mention_comment_id`` discussion.

        A task queued before it carried ``mention_note_id`` takes the note at ``fallback_index``, as runs did then.
        """
        if self.mention_note_id is None:
            return discussion.notes[fallback_index]
        for note in discussion.notes:
            if note.id == self.mention_note_id:
                return note
        raise LookupError(f"Note {self.mention_note_id} is not in discussion {discussion.id}.")

    @staticmethod
    def _append_footer(body: str, footer: str | None) -> str:
        if not footer:
            return body
        return f"{body.rstrip()}\n\n{footer.lstrip()}"

    def _question_comment(self, outcome: RunOutcome, *, asker: str) -> str | None:
        """The comment posting the question the run ended on, mentioning ``asker`` with how to answer it; ``None``
        without a question."""
        if outcome.agent_result["question"] is None:
            return None
        footer = render_to_string(
            "webhooks/ask_user_question.txt", {"asker": asker, "bot_username": self.client.current_user.username}
        )
        return self._append_footer(outcome.response_text, footer.strip())

    def _claim_unable_note(self) -> bool:
        """Idempotency guard for failure comments ("unable to address" and "can't run yet").

        Two handlers can post one: the run's ``on_failure`` hook, which knows whether a draft was published, and
        the catch-all in the ``address_*`` entry point, which also covers failures before the run starts
        (fetching the mention comment). When the run itself fails, both fire, the hook first. Returns ``True``
        the first time (caller posts) and ``False`` on every later call (caller skips).
        """
        if self._unable_note_posted:
            return False
        self._unable_note_posted = True
        return True

    async def _lock_policy(self) -> LockPolicy:
        """Wait for the session's slot, or run unlocked on a thread's first turn: the callback creates the
        ``Session`` row just after enqueueing this run, so the row may not exist yet."""
        return wait_unless_sessionless(
            self.thread_id,
            session_exists=await Session.objects.filter(pk=self.thread_id).aexists(),
            holder_id=f"webhook-{uuid.uuid4().hex}",
        )
