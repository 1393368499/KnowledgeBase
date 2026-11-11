from django.urls import path
from . import views

app_name = "voice"
urlpatterns = [
    path("endpoint/", views.EndpointView.as_view()),
    path("devices/", views.DevicesView.as_view()),
    path("devices/<uuid:device_id>/", views.DeviceDetailView.as_view()),
    path("devices/<uuid:device_id>/rotate-token/", views.RotateTokenView.as_view()),
    path("events/", views.EventsView.as_view()),
    path("device/config/", views.ConfigView.as_view()),
    path("device/heartbeat/", views.ConfigView.as_view()),
    path("device/standby/", views.StandbyView.as_view()),
    path("device/tasks/", views.TasksView.as_view()),
    path("device/tasks/<int:conversation_id>/", views.TaskDetailView.as_view()),
    path("device/tasks/<int:conversation_id>/rename/", views.TaskRenameView.as_view()),
    path("device/tasks/<int:conversation_id>/delete/", views.TaskDeleteView.as_view()),
    path("device/task-selection/", views.TaskSelectionView.as_view()),
    path("device/task-menu/<int:number>/", views.TaskMenuView.as_view()),
    path("device/task-home/", views.TaskHomeView.as_view()),
    path("device/turns/", views.UploadView.as_view()),
    path("device/turns/<uuid:turn_id>/", views.TurnView.as_view()),
    path("device/turns/<uuid:turn_id>/retry/", views.RetryView.as_view()),
    path("device/turns/<uuid:turn_id>/cancel/", views.CancelTurnView.as_view()),
    path("device/turns/<uuid:turn_id>/pages/<str:view>/<int:number>/", views.PageView.as_view()),
    path("device/turns/<uuid:turn_id>/pages/<str:view>/batch/<int:number>/", views.PageBatchView.as_view()),
]
