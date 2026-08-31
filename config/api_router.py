"""Router del API v1. Se monta bajo /api/v1/ en config.urls."""
from rest_framework.routers import DefaultRouter

from apps.accounts.api import AccountViewSet
from apps.transactions.api import (
    CategoryBudgetViewSet,
    CategoryViewSet,
    TransactionViewSet,
)
from apps.workspaces.api import MembershipViewSet, WorkspaceViewSet

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("accounts", AccountViewSet, basename="account")
router.register("categories", CategoryViewSet, basename="category")
router.register("transactions", TransactionViewSet, basename="transaction")
router.register("category-budgets", CategoryBudgetViewSet, basename="categorybudget")

urlpatterns = router.urls
