from django.urls import path

from allauth.account.views import SignupView

from accounts.views import LoginView

urlpatterns = [
    path(route="signup/", view=SignupView.as_view(), name="account_signup"),
    path(route="login/", view=LoginView.as_view(), name="account_login"),
]
