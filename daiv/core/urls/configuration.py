from django.urls import path

from core.views import SiteConfigurationView

urlpatterns = [path("", SiteConfigurationView.as_view(), name="site_configuration")]
