from django.urls import path
from . import views

app_name = "voice"
urlpatterns = [
    path("devices/", views.DevicesView.as_view()),
    path("devices/<uuid:device_id>/", views.DeviceDetailView.as_view()),
    path("devices/<uuid:device_id>/rotate-token/", views.RotateTokenView.as_view()),
    path("events/", views.EventsView.as_view()),
    path("device/config/", views.ConfigView.as_view()),
    path("device/heartbeat/", views.ConfigView.as_view()),
    path("device/turns/", views.UploadView.as_view()),
    path("device/turns/<uuid:turn_id>/", views.TurnView.as_view()),
    path("device/turns/<uuid:turn_id>/retry/", views.RetryView.as_view()),
    path("device/turns/<uuid:turn_id>/cancel/", views.CancelTurnView.as_view()),
    path("device/turns/<uuid:turn_id>/pages/<str:view>/<int:number>/", views.PageView.as_view()),
    path("device/turns/<uuid:turn_id>/pages/<str:view>/batch/<int:number>/", views.PageBatchView.as_view()),
]
