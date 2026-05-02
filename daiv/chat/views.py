from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseGone, QueryDict
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, View

from activity.models import Activity
from asgiref.sync import async_to_sync

from chat.models import ChatThread
from chat.repo_state import aget_existing_mr_payload, mr_to_payload
from chat.turns import build_turns
from chat.usage import aggregate_messages_usage
from core.checkpointer import open_checkpointer
from core.htmx import is_htmx

logger = logging.getLogger(__name__)

_LIST_PAGINATE_BY = 25


async def _ahydrate(thread_id: str) -> tuple[list[Any], bool, dict | None]:
    """Return (messages, expired, merge_request_payload) for a thread."""
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return [], True, None
    channel_values = (tup.checkpoint or {}).get("channel_values", {})
    messages = channel_values.get("messages", [])
    return messages, False, mr_to_payload(channel_values.get("merge_request"))


def _filtered_thread_qs(request):
    user = request.user
    if user.is_admin and request.GET.get("all") == "1":
        qs = ChatThread.objects.all()
    else:
        qs = ChatThread.objects.for_user(user)
    if q := request.GET.get("q", "").strip():
        qs = qs.filter(title__icontains=q)
    if repo_id := request.GET.get("repo_id", "").strip():
        qs = qs.filter(repo_id=repo_id)
    status = request.GET.get("status", "").strip().lower()
    if status == "active":
        qs = qs.filter(active_run_id__isnull=False)
    elif status == "idle":
        qs = qs.filter(active_run_id__isnull=True)
    return qs


def _thread_list_extras(request, selected_thread_id: str = "") -> dict:
    user = request.user
    base_qs = ChatThread.objects.all() if user.is_admin else ChatThread.objects.for_user(user)
    keep = ["q", "repo_id", "status", "all"]
    qd = QueryDict(mutable=True)
    for k in keep:
        if v := request.GET.get(k):
            qd[k] = v
    filter_qs = qd.urlencode()
    return {
        "available_repos": list(base_qs.values_list("repo_id", flat=True).distinct().order_by("repo_id")),
        "status_choices": [("", _("All")), ("active", _("Active")), ("idle", _("Idle"))],
        "selected_thread_id": selected_thread_id,
        "filter_qs": filter_qs,
        "filter_signature": filter_qs,
    }


def _thread_list_context(request, selected_thread_id: str = "") -> dict:
    """Build the sidebar thread-list context for any workspace full-page render."""
    paginator = Paginator(_filtered_thread_qs(request), _LIST_PAGINATE_BY)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    return {
        "threads": page_obj.object_list,
        "page_obj": page_obj,
        "is_paginated": page_obj.has_other_pages(),
        "paginator": paginator,
        **_thread_list_extras(request, selected_thread_id),
    }


class ChatThreadListView(LoginRequiredMixin, ListView):
    model = ChatThread
    template_name = "chat/chat_list.html"
    context_object_name = "threads"
    paginate_by = _LIST_PAGINATE_BY

    def get_template_names(self):
        if self.request.GET.get("fragment") == "rows":
            return ["chat/_thread_rows.html"]
        if is_htmx(self.request):
            return ["chat/_thread_list.html"]
        return [self.template_name]

    def get_queryset(self):
        return _filtered_thread_qs(self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(_thread_list_extras(self.request, selected_thread_id=self.request.GET.get("selected", "")))
        return ctx


class ChatThreadDetailView(LoginRequiredMixin, DetailView):
    """Renders the chat page for a specific thread, or the empty state when no
    ``thread_id`` URL kwarg is present (the ``chat_new`` route).
    """

    model = ChatThread
    template_name = "chat/chat_detail.html"
    context_object_name = "thread"
    pk_url_kwarg = "thread_id"

    def get_template_names(self):
        if is_htmx(self.request):
            return ["chat/_detail.html"]
        return [self.template_name]

    def get_queryset(self):
        return ChatThread.objects.for_user(self.request.user)

    def get_object(self, queryset=None):
        if "thread_id" not in self.kwargs:
            return None
        return super().get_object(queryset)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        thread = ctx.setdefault("thread", None)
        ctx["initial_pane"] = "detail" if thread is not None else "list"

        if not is_htmx(self.request):
            try:
                selected = str(thread.thread_id) if thread else ""
                ctx.update(_thread_list_context(self.request, selected_thread_id=selected))
            except Exception:
                tid = thread.thread_id if thread else "new"
                logger.exception("Failed to load sidebar context for thread %s", tid)

        if thread is None:
            ctx.update({
                "turns": [],
                "expired": False,
                "active_run_id": "",
                "merge_request": None,
                "usage_summary": {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": None,
                    "by_model": {},
                },
            })
            return ctx
        messages_history, expired, merge_request = async_to_sync(_ahydrate)(thread.thread_id)
        if merge_request is None:
            merge_request = async_to_sync(aget_existing_mr_payload)(thread.repo_id, thread.ref)
        ctx["turns"] = build_turns(messages_history)
        ctx["expired"] = expired
        ctx["active_run_id"] = thread.active_run_id
        ctx["merge_request"] = merge_request
        ctx["usage_summary"] = aggregate_messages_usage(messages_history).to_dict()
        return ctx


class ChatThreadFromActivityView(LoginRequiredMixin, View):
    """Bridge: create (or reuse) a ChatThread for an activity and redirect to it."""

    def post(self, request, *, activity_id):
        activity = get_object_or_404(Activity, pk=activity_id, user=request.user)
        if not activity.thread_id:
            raise Http404

        messages, expired, _mr = async_to_sync(_ahydrate)(activity.thread_id)
        if expired:
            return HttpResponseGone("This run's state has expired. Start a fresh chat from its prompt.")

        thread, _ = async_to_sync(ChatThread.aget_or_create_from_activity)(request.user, activity)
        return redirect("chat_detail", thread_id=thread.thread_id)
