"""Router del API v1. Se monta bajo /api/v1/ en config.urls."""
from rest_framework.routers import DefaultRouter

from apps.accounts.api import WalletViewSet
from apps.billing.api import PlanViewSet
from apps.email_import.api import BankEmailSchemaViewSet, EmailImportLogViewSet
from apps.loyalty.api import (
    BankViewSet,
    CardProductViewSet,
    CategoryTypeViewSet,
    LoyaltyCategoryRateViewSet,
    MerchantViewSet,
    LoyaltyEarningViewSet,
    LoyaltyProgramViewSet,
)
from apps.notifications.api import NotificationViewSet, PushDeviceViewSet
from apps.quickadd.api import PersonalAccessTokenViewSet
from apps.reports.api import MonthlySnapshotViewSet
from apps.support.api import SupportTicketViewSet
from apps.transactions.api import (
    CategoryBudgetViewSet,
    CategoryViewSet,
    InstallmentPurchaseViewSet,
    PersonViewSet,
    RecurringExpenseViewSet,
    TagViewSet,
    TransactionViewSet,
)
from apps.workspaces.api import (
    ExchangeRateViewSet,
    InvitationViewSet,
    MembershipViewSet,
    WorkspaceViewSet,
)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("invitations", InvitationViewSet, basename="invitation")
router.register("exchange-rates", ExchangeRateViewSet, basename="exchangerate")

router.register("wallets", WalletViewSet, basename="wallet")

router.register("categories", CategoryViewSet, basename="category")
router.register("tags", TagViewSet, basename="tag")
router.register("transactions", TransactionViewSet, basename="transaction")
router.register("people", PersonViewSet, basename="person")
router.register("category-budgets", CategoryBudgetViewSet, basename="categorybudget")
router.register("recurring-expenses", RecurringExpenseViewSet, basename="recurringexpense")
router.register("installment-purchases", InstallmentPurchaseViewSet, basename="installmentpurchase")

router.register("monthly-snapshots", MonthlySnapshotViewSet, basename="monthlysnapshot")

router.register("bank-email-schemas", BankEmailSchemaViewSet, basename="bankemailschema")
router.register("email-import-logs", EmailImportLogViewSet, basename="emailimportlog")

router.register("banks", BankViewSet, basename="bank")
router.register("category-types", CategoryTypeViewSet, basename="categorytype")
router.register("card-products", CardProductViewSet, basename="cardproduct")
router.register("loyalty-programs", LoyaltyProgramViewSet, basename="loyaltyprogram")
router.register("loyalty-merchants", MerchantViewSet, basename="loyaltymerchant")
router.register("loyalty-category-rates", LoyaltyCategoryRateViewSet, basename="loyaltycategoryrate")
router.register("loyalty-earnings", LoyaltyEarningViewSet, basename="loyaltyearning")

router.register("personal-tokens", PersonalAccessTokenViewSet, basename="personaltoken")

router.register("push-devices", PushDeviceViewSet, basename="pushdevice")
router.register("notifications", NotificationViewSet, basename="notification")

router.register("plans", PlanViewSet, basename="plan")

router.register("support-tickets", SupportTicketViewSet, basename="supportticket")

urlpatterns = router.urls
