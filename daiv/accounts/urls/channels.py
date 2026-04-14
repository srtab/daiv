from django.urls import path

from notifications.views import UserChannelsView

urlpatterns = [path("", UserChannelsView.as_view(), name="user_channels")]
