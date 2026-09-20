from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'fires', views.FireHotspotViewSet, basename='firehotspot')
router.register(r'burns', views.BurnAreaViewSet, basename='burnarea')
router.register(r'jobs', views.ProcessingJobViewSet, basename='processingjob')
router.register(r'reports', views.BurnReportViewSet, basename='burnreport')

urlpatterns = [
    path('', include(router.urls)),
    path('chat/', views.chat_view, name='chat'),
    path('fetch-fires/', views.fetch_fires_view, name='fetch-fires'),
    path('map-burns/', views.map_burns_view, name='map-burns'),
    path('stats/', views.overview_stats, name='overview-stats'),
]
