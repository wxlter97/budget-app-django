"""Resolución de plan y los dos gates que ya quedaron conectados:
límite de workspaces poseídos y límite de miembros por workspace."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.billing.models import Plan, Subscription
from apps.billing.services import (
    can_add_member,
    can_own_another_workspace,
    plan_for_user,
    plan_for_workspace,
)
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

WORKSPACES = "/api/v1/workspaces/"
MEMBERSHIPS = "/api/v1/memberships/"
WORKSPACE_HEADER = "HTTP_X_WORKSPACE_ID"


def make_plans():
    free = Plan.objects.create(
        code="free", name="Gratis", is_default=True,
        max_workspaces_owned=1, max_members_per_workspace=2,
    )
    pro = Plan.objects.create(
        code="pro", name="Pro",
        max_workspaces_owned=None, max_members_per_workspace=None,
    )
    return free, pro


class PlanResolutionTests(APITestCase):
    def setUp(self):
        self.free, self.pro = make_plans()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_user_without_subscription_gets_default_plan(self):
        self.assertEqual(plan_for_user(self.user).code, "free")

    def test_user_with_active_subscription_gets_its_plan(self):
        Subscription.objects.create(user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        self.assertEqual(plan_for_user(self.user).code, "pro")

    def test_canceled_subscription_does_not_count(self):
        Subscription.objects.create(user=self.user, plan=self.pro, status=Subscription.STATUS_CANCELED)
        self.assertEqual(plan_for_user(self.user).code, "free")

    def test_expired_period_end_does_not_count_even_if_status_active(self):
        from datetime import timedelta

        from django.utils import timezone

        Subscription.objects.create(
            user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE,
            current_period_end=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(plan_for_user(self.user).code, "free")

    def test_past_due_still_counts_as_in_force_within_grace_period(self):
        from datetime import timedelta

        from django.utils import timezone

        Subscription.objects.create(
            user=self.user, plan=self.pro, status=Subscription.STATUS_PAST_DUE,
            current_period_end=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(plan_for_user(self.user).code, "pro")

    def test_lifetime_subscription_without_period_end_never_expires(self):
        Subscription.objects.create(
            user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE, current_period_end=None,
        )
        self.assertEqual(plan_for_user(self.user).code, "pro")

    def test_workspace_plan_is_the_owner_s_plan(self):
        ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=ws, user=self.user, role=Membership.ROLE_OWNER)
        Subscription.objects.create(user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        self.assertEqual(plan_for_workspace(ws).code, "pro")

    def test_invited_member_does_not_need_their_own_subscription(self):
        # Cobramos por workspace via el owner, no por asiento.
        owner = self.user
        member = User.objects.create_user("bob", "bob@example.com", "pw")
        ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=ws, user=member, role=Membership.ROLE_MEMBER)
        Subscription.objects.create(user=owner, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        # El plan del workspace es Pro aunque `member` nunca haya pagado nada.
        self.assertEqual(plan_for_workspace(ws).code, "pro")


class UnseededEnvironmentTests(APITestCase):
    """Sin ningún Plan cargado (p. ej. antes de correr `seed_billing_plans`
    por primera vez), los gates deben fallar ABIERTOS -- nunca 500 en el
    flujo normal de crear un workspace."""

    def setUp(self):
        self.assertFalse(Plan.objects.exists())
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_can_own_another_workspace_defaults_to_true(self):
        self.assertTrue(can_own_another_workspace(self.user))

    def test_can_add_member_defaults_to_true(self):
        ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=ws, user=self.user, role=Membership.ROLE_OWNER)
        self.assertTrue(can_add_member(ws))

    def test_creating_a_workspace_does_not_500(self):
        resp = self.client.post(WORKSPACES, {"name": "Casa"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class WorkspaceOwnershipLimitTests(APITestCase):
    """Gate de `WorkspaceViewSet.perform_create` -- apps/workspaces/api.py."""

    def setUp(self):
        self.free, self.pro = make_plans()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_free_user_can_create_first_workspace(self):
        self.assertTrue(can_own_another_workspace(self.user))
        resp = self.client.post(WORKSPACES, {"name": "Casa"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_free_user_cannot_create_second_workspace(self):
        self.client.post(WORKSPACES, {"name": "Casa"})
        self.assertFalse(can_own_another_workspace(self.user))
        resp = self.client.post(WORKSPACES, {"name": "Otro"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pro_user_can_create_multiple_workspaces(self):
        Subscription.objects.create(user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        self.client.post(WORKSPACES, {"name": "Casa"})
        resp = self.client.post(WORKSPACES, {"name": "Laburo"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_being_a_member_elsewhere_does_not_count_against_the_limit(self):
        # El límite es de workspaces que POSEE (owner), no de los que integra.
        other_owner = User.objects.create_user("bob", "bob@example.com", "pw")
        shared = Workspace.objects.create(name="Compartido")
        Membership.objects.create(workspace=shared, user=other_owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=shared, user=self.user, role=Membership.ROLE_MEMBER)

        resp = self.client.post(WORKSPACES, {"name": "Mío"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class WorkspaceMemberLimitTests(APITestCase):
    """Gate de `MembershipViewSet.create` y `InvitationViewSet.accept`."""

    def setUp(self):
        self.free, self.pro = make_plans()
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.workspace, user=self.owner, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.owner)

    def _invite(self, email):
        return self.client.post(
            MEMBERSHIPS, {"email": email}, **{WORKSPACE_HEADER: str(self.workspace.id)}
        )

    def test_free_plan_allows_up_to_two_members(self):
        # El owner ya cuenta como 1 -- el free permite 1 más.
        self.assertTrue(can_add_member(self.workspace))
        User.objects.create_user("bob", "bob@example.com", "pw")
        resp = self._invite("bob@example.com")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_free_plan_rejects_a_third_member(self):
        User.objects.create_user("bob", "bob@example.com", "pw")
        self._invite("bob@example.com")
        self.assertFalse(can_add_member(self.workspace))

        User.objects.create_user("carol", "carol@example.com", "pw")
        resp = self._invite("carol@example.com")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pro_plan_has_no_member_limit(self):
        Subscription.objects.create(user=self.owner, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        for i in range(5):
            User.objects.create_user(f"user{i}", f"user{i}@example.com", "pw")
            resp = self._invite(f"user{i}@example.com")
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class RecurringExpenseLimitTests(APITestCase):
    """Gate de `max_active_recurring` -- `RecurringExpenseSerializer.validate`
    (apps/transactions/api.py). Antes del 22-sep-2026 este límite estaba
    declarado en `Plan` pero sin ningún chequeo real (ver `can_add_recurring`,
    apps/billing/services.py)."""

    URL = "/api/v1/recurring-expenses/"
    HEADER = "HTTP_X_WORKSPACE_ID"

    def setUp(self):
        from apps.accounts.models import Wallet
        from apps.transactions.models import Category

        self.free = Plan.objects.create(
            code="free", name="Gratis", is_default=True, max_active_recurring=0,
        )
        self.plus = Plan.objects.create(code="plus", name="Plus", max_active_recurring=1)
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta")
        self.category = Category.objects.create(
            workspace=self.ws, name="Netflix", type=Category.TYPE_EXPENSE
        )
        self.client.force_authenticate(self.owner)
        self.headers = {self.HEADER: str(self.ws.id)}

    def _payload(self, **overrides):
        payload = {
            "category": str(self.category.id), "wallet": str(self.wallet.id),
            "amount": "12.99", "frequency": "monthly", "next_due_date": "2026-10-01",
        }
        payload.update(overrides)
        return payload

    def test_free_cannot_create_an_active_recurring(self):
        resp = self.client.post(self.URL, self._payload(), format="json", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertIn("Plus", str(resp.data))

    def test_free_can_create_an_inactive_recurring(self):
        # Guardado en pausa: no suma a la cuenta de activos (mismo criterio
        # que "editar uno ya activo sin tocar is_active").
        resp = self.client.post(
            self.URL, self._payload(is_active=False), format="json", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_plus_can_create_up_to_its_limit(self):
        Subscription.objects.create(user=self.owner, plan=self.plus, status=Subscription.STATUS_ACTIVE)
        resp = self.client.post(self.URL, self._payload(), format="json", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_plus_rejects_going_over_its_limit(self):
        from apps.transactions.models import RecurringExpense

        Subscription.objects.create(user=self.owner, plan=self.plus, status=Subscription.STATUS_ACTIVE)
        RecurringExpense.objects.create(
            workspace=self.ws, type=RecurringExpense.TYPE_EXPENSE, category=self.category,
            wallet=self.wallet, amount="12.99", frequency="monthly", next_due_date="2026-10-01",
        )
        resp = self.client.post(self.URL, self._payload(), format="json", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)

    def test_reactivating_a_paused_one_counts_against_the_limit(self):
        from apps.transactions.models import RecurringExpense

        Subscription.objects.create(user=self.owner, plan=self.plus, status=Subscription.STATUS_ACTIVE)
        active = RecurringExpense.objects.create(
            workspace=self.ws, type=RecurringExpense.TYPE_EXPENSE, category=self.category,
            wallet=self.wallet, amount="12.99", frequency="monthly", next_due_date="2026-10-01",
        )
        paused = RecurringExpense.objects.create(
            workspace=self.ws, type=RecurringExpense.TYPE_EXPENSE, category=self.category,
            wallet=self.wallet, amount="5.00", frequency="monthly", next_due_date="2026-10-01",
            is_active=False,
        )
        resp = self.client.patch(
            f"{self.URL}{paused.id}/", {"is_active": True}, format="json", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        active.is_active = False
        active.save(update_fields=["is_active"])
        resp = self.client.patch(
            f"{self.URL}{paused.id}/", {"is_active": True}, format="json", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_editing_an_already_active_one_is_not_blocked(self):
        from apps.transactions.models import RecurringExpense

        active = RecurringExpense.objects.create(
            workspace=self.ws, type=RecurringExpense.TYPE_EXPENSE, category=self.category,
            wallet=self.wallet, amount="12.99", frequency="monthly", next_due_date="2026-10-01",
        )
        resp = self.client.patch(
            f"{self.URL}{active.id}/", {"amount": "15.00"}, format="json", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
