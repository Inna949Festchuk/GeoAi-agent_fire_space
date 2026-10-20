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
    path('sentinel2/search/', views.sentinel2_search_view, name='sentinel2-search'),
    path('stats/', views.overview_stats, name='overview-stats'),
    path('validation/run/', views.validation_run_view, name='validation-run'),
    path('validation/prepare/', views.validation_prepare_view, name='validation-prepare'),
    path('validation/status/', views.validation_status_view, name='validation-status'),
]
