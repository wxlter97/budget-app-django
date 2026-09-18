"""Insights de comportamiento de gasto (`services.behavior_insights`) --
cada patrón se prueba con datos armados a mano para cruzar (o no) su propio
umbral, no con datos aleatorios: la idea es que cada test documente qué
hace falta para que el patrón dispare.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Wallet
from apps.reports.services import behavior_insights
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

TODAY = dt.date(2026, 3, 31)


def _kinds(insights):
    return {i["dedupe_key"].split(":")[1] for i in insights}


class BehaviorInsightsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING,
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.fun = Category.objects.create(
            workspace=cls.ws, name="Ocio", type=Category.TYPE_EXPENSE
        )
        cls.salary = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )

    def _txn(self, date, amount, *, category=None, type_=Transaction.TYPE_EXPENSE):
        # `Transaction.save()` deriva `type` de `category.type` en cuanto hay
        # categoría (ver apps/transactions/models.py) -- por eso el default
        # de categoría depende de `type_`, no siempre es `self.food`.
        if category is None:
            category = self.salary if type_ == Transaction.TYPE_INCOME else self.food
        Transaction.objects.create(
            wallet=self.wallet, category=category, amount=Decimal(str(amount)),
            date=date, type=type_,
        )

    def test_no_insights_with_too_little_history(self):
        for i in range(10):
            self._txn(TODAY - dt.timedelta(days=i), "10.00")
        self.assertEqual(behavior_insights(self.ws, self.user, today=TODAY), [])

    def test_no_insights_with_flat_spending(self):
        # Mismo monto (por encima del umbral de "hormiga") todos los días
        # sin excepción, cubriendo de sobra los meses de línea de base --
        # ningún patrón debería disparar.
        for i in range(130):
            self._txn(TODAY - dt.timedelta(days=i), "20.00", category=self.food)
        self.assertEqual(behavior_insights(self.ws, self.user, today=TODAY), [])

    def test_weekend_insight(self):
        for i in range(70):
            date = TODAY - dt.timedelta(days=i)
            amount = "40.00" if date.weekday() >= 5 else "10.00"
            self._txn(date, amount)
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("weekend", kinds)

    def test_post_income_insight(self):
        # Cobra cada 15 días, y los 3 días siguientes (calendario) gasta
        # mucho más que el resto del tiempo. `i` cuenta hacia atrás desde
        # HOY, así que "después" de la fecha en `i=15k` son los `i` MENORES
        # (15k-1, 15k-2, 15k-3), no los mayores.
        for i in range(90):
            date = TODAY - dt.timedelta(days=i)
            p = i % 15
            if p == 0:
                self._txn(date, "1000.00", type_=Transaction.TYPE_INCOME)
            elif p >= 12:
                self._txn(date, "80.00")
            else:
                self._txn(date, "10.00")
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("post_income", kinds)

    def test_small_purchases_insight(self):
        # Compras hormiga (< $15) todos los días + una compra grande para
        # tener suficiente historia -- que la hormiga sume $50+ este mes.
        for i in range(31):
            date = TODAY - dt.timedelta(days=i)
            self._txn(date, "3.00")
        # Historia previa para pasar el umbral de días mínimos.
        for i in range(31, 40):
            self._txn(TODAY - dt.timedelta(days=i), "3.00")
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("small_purchases", kinds)

    def test_small_purchases_insight_not_triggered_below_threshold(self):
        for i in range(40):
            self._txn(TODAY - dt.timedelta(days=i), "3.00" if i < 3 else "20.00")
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertNotIn("small_purchases", kinds)

    def test_peak_day_insight(self):
        for i in range(60):
            date = TODAY - dt.timedelta(days=i)
            amount = "50.00" if date.weekday() == 3 else "10.00"  # jueves
            self._txn(date, amount)
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("peak_day", kinds)

    def test_category_spike_insight(self):
        # Meses anteriores: gasto bajo y estable en Ocio. Este mes: mucho
        # más alto en Ocio -- el resto (Comida) se mantiene igual siempre.
        for i in range(120):
            date = TODAY - dt.timedelta(days=i)
            self._txn(date, "10.00", category=self.food)
        for i in range(31):  # marzo (mes en curso)
            self._txn(TODAY - dt.timedelta(days=i), "30.00", category=self.fun)
        for i in range(31, 120):  # meses previos
            self._txn(TODAY - dt.timedelta(days=i), "2.00", category=self.fun)
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("category_spike", kinds)

    def test_frequency_spike_insight(self):
        # Mismo monto chico siempre en Ocio, pero MUCHAS más compras este
        # mes que en los meses previos (la plata no cambia, la frecuencia sí).
        for i in range(31):  # marzo: todos los días
            self._txn(TODAY - dt.timedelta(days=i), "5.00", category=self.fun)
        for i in range(31, 120):  # antes: una de cada 10 días
            if i % 10 == 0:
                self._txn(TODAY - dt.timedelta(days=i), "5.00", category=self.fun)
        # Comida estable, para que no contamine como gasto/frecuencia base.
        for i in range(120):
            self._txn(TODAY - dt.timedelta(days=i), "10.00", category=self.food)
        kinds = _kinds(behavior_insights(self.ws, self.user, today=TODAY))
        self.assertIn("frequency_spike", kinds)
