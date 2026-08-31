"""Router del API v1. Se monta bajo /api/v1/ en config.urls."""
from rest_framework.routers import DefaultRouter

from apps.accounts.api import (
    AccountViewSet,
    AssetViewSet,
    DebtViewSet,
    LiabilityViewSet,
)
from apps.email_import.api import BankEmailSchemaViewSet, EmailImportLogViewSet
from apps.reports.api import MonthlySnapshotViewSet
from apps.savings.api import ReserveFundViewSet, SavingsGoalViewSet
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

router.register("accounts", AccountViewSet, basename="account")
router.register("assets", AssetViewSet, basename="asset")
router.register("liabilities", LiabilityViewSet, basename="liability")
router.register("debts", DebtViewSet, basename="debt")

router.register("categories", CategoryViewSet, basename="category")
router.register("transactions", TransactionViewSet, basename="transaction")
router.register("category-budgets", CategoryBudgetViewSet, basename="categorybudget")
router.register("recurring-expenses", RecurringExpenseViewSet, basename="recurringexpense")
router.register("installment-purchases", InstallmentPurchaseViewSet, basename="installmentpurchase")

router.register("savings-goals", SavingsGoalViewSet, basename="savingsgoal")
router.register("reserve-funds", ReserveFundViewSet, basename="reservefund")

router.register("monthly-snapshots", MonthlySnapshotViewSet, basename="monthlysnapshot")

router.register("bank-email-schemas", BankEmailSchemaViewSet, basename="bankemailschema")
router.register("email-import-logs", EmailImportLogViewSet, basename="emailimportlog")

urlpatterns = router.urls
