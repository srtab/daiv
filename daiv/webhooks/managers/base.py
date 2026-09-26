import logging
import uuid
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from sessions.executor.lock import LOCK_WAIT_TIMEOUT_S, NoLock, Wait
from sessions.models import Session

from codebase.base import GitPlatform
from codebase.clients import RepoClient

if TYPE_CHECKING:
    from sessions.executor.lock import LockPolicy
    from sessions.executor.spec import RunOutcome

logger = logging.getLogger("daiv.managers")


class BaseManager:
    """
    Base class for the webhook managers: each builds one agent run's ``RunSpec`` and posts its outcome back on
    the issue or merge request.
    """

    _unable_note_posted: bool = False
    """ Backing flag for :meth:`_claim_unable_note`; see that method for the rationale. """

    def __init__(self, *, repo_id: str, thread_id: str, mention_comment_id: str | None = None):
        self.repo_id = repo_id
        self.thread_id = thread_id
        self.mention_comment_id = mention_comment_id
        self.client = RepoClient.create_instance()

    @property
    def reply_to_id(self) -> str | None:
        """The mention a failure comment answers under; GitHub can't reply to a comment, so only GitLab gets one."""
        return self.mention_comment_id if self.client.git_platform == GitPlatform.GITLAB else None

    @staticmethod
    def _append_footer(body: str, footer: str | None) -> str:
        if not footer:
            return body
        return f"{body.rstrip()}\n\n{footer.lstrip()}"

    def _question_comment(self, outcome: RunOutcome) -> str | None:
        """The comment posting the question the run ended on, with how to answer it; ``None`` without a question."""
        if outcome.agent_result["question"] is None:
            return None
        footer = render_to_string("webhooks/ask_user_question.txt", {"bot_username": self.client.current_user.username})
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
        ``Session`` row just after enqueueing this run, so the row may not exist yet, and then nothing else can
        hold the slot either."""
        if await Session.objects.filter(pk=self.thread_id).aexists():
            return Wait(holder_id=f"webhook-{uuid.uuid4().hex}", timeout_s=LOCK_WAIT_TIMEOUT_S)
        logger.warning("webhook run: no session row for thread_id=%s; running without lock", self.thread_id)
        return NoLock()
