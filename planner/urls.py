from django.urls import path

from .views import health_view, plan_trip_view

urlpatterns = [
    path("health/", health_view, name="health"),
    path("plan/", plan_trip_view, name="plan"),
]

