from django.urls import path

from accounts.views import UserCreateView, UserDeleteView, UserListView, UserPickerView, UserUpdateView

urlpatterns = [
    path("", UserListView.as_view(), name="user_list"),
    path("create/", UserCreateView.as_view(), name="user_create"),
    path("<int:pk>/edit/", UserUpdateView.as_view(), name="user_update"),
    path("<int:pk>/delete/", UserDeleteView.as_view(), name="user_delete"),
    path("picker/", UserPickerView.as_view(), name="picker_users"),
]
