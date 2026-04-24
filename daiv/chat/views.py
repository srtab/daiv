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
from chat.turns import build_turns
from core.checkpointer import open_checkpointer


async def _ahydrate(thread_id: str) -> tuple[list[Any], bool]:
    """Return (messages, expired) for a thread."""
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return [], True
    messages = (tup.checkpoint or {}).get("channel_values", {}).get("messages", [])
    return messages, False


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
            ctx.update({"turns": [], "expired": False})
            return ctx
        messages_history, expired = async_to_sync(_ahydrate)(thread.thread_id)
        ctx["turns"] = build_turns(messages_history)
        ctx["expired"] = expired
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
        activity = get_object_or_404(Activity, pk=activity_id, user=request.user)
        if not activity.thread_id:
            raise Http404

        messages, expired = async_to_sync(_ahydrate)(activity.thread_id)
        if expired:
            return HttpResponseGone("This run's state has expired. Start a fresh chat from its prompt.")

        thread, _ = async_to_sync(ChatThread.aget_or_create_from_activity)(request.user, activity)
        return redirect("chat_detail", thread_id=thread.thread_id)
