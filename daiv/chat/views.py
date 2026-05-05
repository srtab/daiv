from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseGone
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from activity.models import Activity
from asgiref.sync import async_to_sync

from accounts.mixins import BreadcrumbMixin
from chat.models import ChatThread
from chat.repo_state import aget_existing_mr_payload, mr_to_payload
from chat.turns import build_turns
from core.checkpointer import open_checkpointer


async def _ahydrate(thread_id: str) -> tuple[list[Any], bool, dict | None]:
    """Return (messages, expired, merge_request_payload) for a thread."""
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return [], True, None
    channel_values = (tup.checkpoint or {}).get("channel_values", {})
    messages = channel_values.get("messages", [])
    return messages, False, mr_to_payload(channel_values.get("merge_request"))


class ChatThreadListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = ChatThread
    template_name = "chat/chat_list.html"
    context_object_name = "threads"
    paginate_by = 25

    def get_queryset(self):
        return ChatThread.objects.for_user(self.request.user)

    def get_breadcrumbs(self):
        return [{"label": "Chat", "url": None}]


class ChatThreadDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    """Renders the chat page for a specific thread, or the empty state when no
    ``thread_id`` URL kwarg is present (the ``chat_new`` route).
    """

    model = ChatThread
    template_name = "chat/chat_detail.html"
    context_object_name = "thread"
    pk_url_kwarg = "thread_id"

    def get_queryset(self):
        return ChatThread.objects.for_user(self.request.user)

    def get_object(self, queryset=None):
        if "thread_id" not in self.kwargs:
            return None
        return super().get_object(queryset)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        thread = ctx.setdefault("thread", None)
        if thread is None:
            ctx.update({"turns": [], "expired": False, "active_run_id": "", "merge_request": None})
            return ctx
        messages_history, expired, merge_request = async_to_sync(_ahydrate)(thread.thread_id)
        if merge_request is None:
            merge_request = async_to_sync(aget_existing_mr_payload)(thread.repo_id, thread.ref)
        ctx["turns"] = build_turns(messages_history)
        ctx["expired"] = expired
        ctx["active_run_id"] = thread.active_run_id
        ctx["merge_request"] = merge_request
        ctx["next_chat"] = self.request.GET.get("next") == "1"
        return ctx

    def get_breadcrumbs(self):
        chat_url = reverse("chat_list")
        thread = getattr(self, "object", None)
        if thread is None:
            return [{"label": "Chat", "url": chat_url}, {"label": "New", "url": None}]
        return [{"label": "Chat", "url": chat_url}, {"label": thread.title or thread.thread_id[:8], "url": None}]


class ChatThreadFromActivityView(LoginRequiredMixin, View):
    """Bridge: create (or reuse) a ChatThread for an activity and redirect to it."""

    def post(self, request, *, activity_id):
        # Mirror ActivityDetailView's visibility (Activity.objects.by_owner) so the
        # button rendered there always works — webhook activities have user=None and
        # are reached via external_username; without this the bridge 404s.
        activity = get_object_or_404(Activity.objects.by_owner(request.user), pk=activity_id)
        if not activity.thread_id:
            raise Http404

        messages, expired, _mr = async_to_sync(_ahydrate)(activity.thread_id)
        if expired:
            return HttpResponseGone("This run's state has expired. Start a fresh chat from its prompt.")

        thread, _ = async_to_sync(ChatThread.aget_or_create_from_activity)(request.user, activity)
        return redirect("chat_detail", thread_id=thread.thread_id)
