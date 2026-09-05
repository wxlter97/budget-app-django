"""Router del API v1. Se monta bajo /api/v1/ en config.urls."""
from rest_framework.routers import DefaultRouter

from apps.accounts.api import WalletViewSet
from apps.email_import.api import BankEmailSchemaViewSet, EmailImportLogViewSet
from apps.notifications.api import PushDeviceViewSet
from apps.quickadd.api import PersonalAccessTokenViewSet
from apps.reports.api import MonthlySnapshotViewSet
from apps.transactions.api import (
    CategoryBudgetViewSet,
    CategoryViewSet,
    InstallmentPurchaseViewSet,
    RecurringExpenseViewSet,
    TransactionViewSet,
)
from apps.workspaces.api import MembershipViewSet, WorkspaceViewSet

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("memberships", MembershipViewSet, basename="membership")

router.register("wallets", WalletViewSet, basename="wallet")

router.register("categories", CategoryViewSet, basename="category")
router.register("transactions", TransactionViewSet, basename="transaction")
router.register("category-budgets", CategoryBudgetViewSet, basename="categorybudget")
router.register("recurring-expenses", RecurringExpenseViewSet, basename="recurringexpense")
router.register("installment-purchases", InstallmentPurchaseViewSet, basename="installmentpurchase")

router.register("monthly-snapshots", MonthlySnapshotViewSet, basename="monthlysnapshot")

router.register("bank-email-schemas", BankEmailSchemaViewSet, basename="bankemailschema")
router.register("email-import-logs", EmailImportLogViewSet, basename="emailimportlog")

router.register("personal-tokens", PersonalAccessTokenViewSet, basename="personaltoken")

router.register("push-devices", PushDeviceViewSet, basename="pushdevice")

urlpatterns = router.urls
