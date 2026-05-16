from django.urls import path

from core.views import SiteConfigurationGroupView, SiteConfigurationIndexView

urlpatterns = [
    path("", SiteConfigurationIndexView.as_view(), name="site_configuration_index"),
    path("<slug:group_key>/", SiteConfigurationGroupView.as_view(), name="site_configuration"),
]
