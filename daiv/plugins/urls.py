from django.urls import path

from plugins.views import (
    PluginAddView,
    PluginDeleteView,
    PluginDetailView,
    PluginGlobalListView,
    PluginListView,
    PluginSetActiveView,
    PluginSetAutoUpdateView,
    PluginSetOptOutView,
    PluginUpdateNowView,
)

app_name = "plugins"

urlpatterns = [
    path("", PluginListView.as_view(), name="list"),
    path("add/", PluginAddView.as_view(), name="add"),
    path("global/", PluginGlobalListView.as_view(), name="global_list"),
    path("<int:pk>/", PluginDetailView.as_view(), name="detail"),
    path("<int:pk>/active/", PluginSetActiveView.as_view(), name="set_active"),
    path("<int:pk>/auto-update/", PluginSetAutoUpdateView.as_view(), name="set_auto_update"),
    path("<int:pk>/update/", PluginUpdateNowView.as_view(), name="update_now"),
    path("<int:pk>/use/", PluginSetOptOutView.as_view(), name="set_opt_out"),
    path("<int:pk>/delete/", PluginDeleteView.as_view(), name="delete"),
]
