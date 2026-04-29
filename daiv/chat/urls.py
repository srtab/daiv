from django.urls import path

from chat.views import ChatThreadDetailView, ChatThreadFromActivityView, ChatThreadListView

urlpatterns = [
    path("", ChatThreadListView.as_view(), name="chat_list"),
    path("new/", ChatThreadDetailView.as_view(), name="chat_new"),
    path("<slug:thread_id>/", ChatThreadDetailView.as_view(), name="chat_detail"),
    path("from-activity/<uuid:activity_id>/", ChatThreadFromActivityView.as_view(), name="chat_from_activity"),
]
