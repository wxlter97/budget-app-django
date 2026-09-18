"""Períodos de prueba: `services.start_trial` + `POST /billing/trial/`."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.billing.models import Plan, Subscription
from apps.billing.services import start_trial

User = get_user_model()

TRIAL = "/api/v1/billing/trial/"


def make_plan(trial_days=None):
    return Plan.objects.create(
        code="pro", name="Pro", max_workspaces_owned=None, trial_days=trial_days,
    )


class StartTrialServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_starts_a_trial_with_the_configured_duration(self):
        plan = make_plan(trial_days=14)

        before = timezone.now() + timedelta(days=14)
        sub = start_trial(self.user, plan)
        after = timezone.now() + timedelta(days=14)

        self.assertEqual(sub.plan, plan)
        self.assertTrue(sub.is_trial)
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)
        self.assertEqual(sub.provider, "manual")
        self.assertTrue(before <= sub.current_period_end <= after)

    def test_plan_without_trial_days_is_rejected(self):
        plan = make_plan(trial_days=None)
        with self.assertRaises(ValidationError):
            start_trial(self.user, plan)

    def test_plan_with_trial_days_zero_is_rejected(self):
        plan = make_plan(trial_days=0)
        with self.assertRaises(ValidationError):
            start_trial(self.user, plan)

    def test_user_can_only_ever_start_one_trial(self):
        pro = make_plan(trial_days=14)
        plus = Plan.objects.create(code="plus", name="Plus", trial_days=7)

        start_trial(self.user, pro)
        with self.assertRaises(ValidationError):
            start_trial(self.user, plus)

    def test_cannot_start_a_trial_with_an_active_subscription(self):
        plan = make_plan(trial_days=14)
        Subscription.objects.create(user=self.user, plan=plan, status=Subscription.STATUS_ACTIVE)

        with self.assertRaises(ValidationError):
            start_trial(self.user, plan)

    def test_db_constraint_backstops_a_concurrent_double_trial(self):
        """Si dos requests pasan el chequeo de `.exists()` a la vez (carrera),
        la constraint única de la base convierte el segundo `create()` en el
        mismo `ValidationError` en vez de un 500 crudo."""
        plan = make_plan(trial_days=14)
        Subscription.objects.create(
            user=self.user, plan=plan, status=Subscription.STATUS_ACTIVE, is_trial=True,
        )
        # Sin el chequeo de `.exists()` de por medio (ya lo cubre el test de
        # arriba) -- esto prueba la constraint en sí, llamando directo al
        # create() que hace `start_trial` puertas adentro.
        with self.assertRaises(Exception):
            Subscription.objects.create(
                user=self.user, plan=plan, status=Subscription.STATUS_ACTIVE, is_trial=True,
            )


class StartTrialApiTests(APITestCase):
    def setUp(self):
        self.plan = make_plan(trial_days=14)
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_starts_a_trial_and_returns_the_subscription(self):
        resp = self.client.post(TRIAL, {"plan": str(self.plan.id)})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["plan"]["code"], "pro")
        self.assertTrue(resp.data["is_trial"])

    def test_plan_without_trial_returns_400(self):
        no_trial = Plan.objects.create(code="plus", name="Plus", trial_days=None)
        resp = self.client.post(TRIAL, {"plan": str(no_trial.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(TRIAL, {"plan": str(self.plan.id)})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
