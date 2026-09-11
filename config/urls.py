from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework_simplejwt.views import TokenVerifyView

from apps.email_import.api import InboundEmailWebhookView
from apps.notifications.api import NotificationPreferenceView
from apps.quickadd.api import QuickAddView
from apps.reports.api import (
    BudgetReportView,
    CashflowView,
    CategoryTrendsView,
    DashboardSummaryView,
    NetWorthView,
    ScheduledView,
)
from apps.reports.dashboard_api import DashboardBalanceView
from apps.users.api import (
    GoogleLinkView,
    GoogleLoginView,
    MeView,
    RegisterView,
    TokenObtainPairThrottledView,
    TokenRefreshThrottledView,
    TwoFactorDisableView,
    TwoFactorEnableView,
    TwoFactorRegenerateBackupCodesView,
    TwoFactorSetupView,
    TwoFactorStatusView,
    TwoFactorVerifyView,
)
from config.api_router import urlpatterns as api_v1_router

api_v1_patterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/google/", GoogleLoginView.as_view(), name="google_login"),
    path("auth/google/link/", GoogleLinkView.as_view(), name="google_link"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/token/", TokenObtainPairThrottledView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshThrottledView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("auth/2fa/", TwoFactorStatusView.as_view(), name="2fa-status"),
    path("auth/2fa/setup/", TwoFactorSetupView.as_view(), name="2fa-setup"),
    path("auth/2fa/enable/", TwoFactorEnableView.as_view(), name="2fa-enable"),
    path("auth/2fa/disable/", TwoFactorDisableView.as_view(), name="2fa-disable"),
    path(
        "auth/2fa/backup-codes/",
        TwoFactorRegenerateBackupCodesView.as_view(),
        name="2fa-backup-codes",
    ),
    path("auth/2fa/verify/", TwoFactorVerifyView.as_view(), name="2fa-verify"),
    path("reports/budget/", BudgetReportView.as_view(), name="report-budget"),
    path("reports/net-worth/", NetWorthView.as_view(), name="report-net-worth"),
    path("reports/cashflow/", CashflowView.as_view(), name="report-cashflow"),
    path("reports/category-trends/", CategoryTrendsView.as_view(), name="report-category-trends"),
    path("reports/summary/", DashboardSummaryView.as_view(), name="report-summary"),
    path("reports/scheduled/", ScheduledView.as_view(), name="report-scheduled"),
    path("dashboard/balance/", DashboardBalanceView.as_view(), name="dashboard-balance"),
    path(
        "email-import/inbound/",
        InboundEmailWebhookView.as_view(),
        name="email-import-inbound",
    ),
    path("quick-add/", QuickAddView.as_view(), name="quick-add"),
    path(
        "notification-preferences/",
        NotificationPreferenceView.as_view(),
        name="notification-preferences",
    ),
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
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
