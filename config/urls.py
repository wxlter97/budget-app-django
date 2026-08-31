from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from apps.reports.api import (
    BudgetReportView,
    CashflowView,
    DashboardSummaryView,
    NetWorthView,
)
from apps.users.api import MeView, RegisterView
from config.api_router import urlpatterns as api_v1_router

api_v1_patterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("reports/budget/", BudgetReportView.as_view(), name="report-budget"),
    path("reports/net-worth/", NetWorthView.as_view(), name="report-net-worth"),
    path("reports/cashflow/", CashflowView.as_view(), name="report-cashflow"),
    path("reports/summary/", DashboardSummaryView.as_view(), name="report-summary"),
    *api_v1_router,
]

urlpatterns = [
    path("healthz/", lambda _request: JsonResponse({"status": "ok"}), name="healthz"),
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1_patterns, "v1"), namespace="v1")),
    # Esquema OpenAPI + Swagger UI
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
