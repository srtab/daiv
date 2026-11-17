from django.urls import path

from blog.feeds import BlogFeed
from blog.views import BlogIndexView, BlogPostView

app_name = "blog"

urlpatterns = [
    path("", BlogIndexView.as_view(), name="index"),
    path("feed/", BlogFeed(), name="feed"),
    path("<slug:slug>/", BlogPostView.as_view(), name="post"),
]
