"""WompiProvider contra respuestas simuladas de la API documentada en docs.wompi.sv."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.billing import providers
from apps.billing.models import Plan, PlanPrice, ProcessedWebhookEvent, Subscription
from apps.billing.providers import WompiError, WompiProvider
from apps.billing.services import apply_webhook_event

User = get_user_model()
SECRET = "secreto-de-prueba"
CREDS = dict(WOMPI_CLIENT_ID="app-id", WOMPI_CLIENT_SECRET=SECRET)
WEBHOOK = "/api/v1/billing/webhooks/wompi/"


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.content = b"" if data is None else json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


def make_price(period=PlanPrice.BILLING_MONTHLY, cents=99):
    plan, _ = Plan.objects.get_or_create(code="pro", defaults={"name": "Pro"})
    return PlanPrice.objects.create(plan=plan, billing_period=period, amount_cents=cents)


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def approved_body(reference="", tx="tx-1", productive=True, result="ExitosaAprobada", amount=0.99):
    return {
        "IdTransaccion": tx, "ResultadoTransaccion": result, "EsProductiva": productive,
        "Monto": amount, "EnlacePago": {"Id": 66, "IdentificadorEnlaceComercio": reference},
        "cliente": {"Nombre": "Ana", "Email": "ana@example.com"},
    }


@override_settings(**CREDS)
class WompiHttpTests(TestCase):
    def setUp(self):
        providers._token_cache.update(value="", expires_at=0.0)
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def _calls(self, post, request):
        """Respuestas simuladas: `post` para el token, `request` para la API."""
        patcher = mock.patch.multiple(
            "apps.billing.providers.requests", post=post, request=request
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _token_ok(self):
        return mock.Mock(return_value=FakeResponse(200, {"access_token": "tok", "expires_in": 3600}))

    def test_the_token_is_requested_once_and_reused(self):
        post = self._token_ok()
        request = mock.Mock(return_value=FakeResponse(200, {"ok": True}))
        self._calls(post, request)
        provider = WompiProvider()
        provider.request_api("GET", "/algo")
        provider.request_api("GET", "/otra")
        self.assertEqual(post.call_count, 1)
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["grant_type"], "client_credentials")
        self.assertEqual(sent["audience"], "wompi_api")
        self.assertEqual(sent["client_id"], "app-id")
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer tok")

    def test_bad_credentials_raise_a_clear_error(self):
        self._calls(mock.Mock(return_value=FakeResponse(401, {"error": "invalid_client"})), mock.Mock())
        with self.assertRaisesRegex(WompiError, "credenciales"):
            WompiProvider().request_api("GET", "/algo")

    def test_missing_credentials_do_not_call_the_network(self):
        post = mock.Mock()
        self._calls(post, mock.Mock())
        with override_settings(WOMPI_CLIENT_ID="", WOMPI_CLIENT_SECRET=""):
            with self.assertRaisesRegex(WompiError, "WOMPI_CLIENT_ID"):
                WompiProvider().request_api("GET", "/algo")
        post.assert_not_called()

    def test_an_error_response_from_the_api_raises(self):
        self._calls(self._token_ok(), mock.Mock(return_value=FakeResponse(400, {"mensaje": "monto inválido"})))
        with self.assertRaisesRegex(WompiError, "400"):
            WompiProvider().request_api("POST", "/EnlacePago", {})

    def test_monthly_checkout_creates_one_recurring_link_per_purchase(self):
        request = mock.Mock(return_value=FakeResponse(200, {
            "idEnlace": "rec-123", "urlEnlace": "https://lk.wompi.sv/abc", "estaProductivo": False,
        }))
        self._calls(self._token_ok(), request)
        price = make_price(PlanPrice.BILLING_MONTHLY, 99)
        sub = Subscription.objects.create(user=self.user, plan=price.plan, plan_price=price, provider="wompi")

        session = WompiProvider().create_checkout(
            user=self.user, plan_price=price, subscription=sub,
            success_url="https://money.wxlter.dev/pro/ok", cancel_url="https://money.wxlter.dev/pro/no",
        )

        self.assertEqual(session.checkout_url, "https://lk.wompi.sv/abc")
        method, url = request.call_args.args[:2]
        self.assertEqual((method, url), ("POST", "https://api.wompi.sv/EnlacePagoRecurrente"))
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["monto"], 0.99)
        self.assertEqual(body["idAplicativo"], "app-id")
        self.assertLessEqual(body["diaDePago"], 28)
        self.assertIn(str(sub.checkout_reference), body["descripcionProducto"])
        sub.refresh_from_db()
        self.assertEqual(sub.external_subscription_id, "rec-123")

    def test_annual_checkout_creates_a_single_payment_link_with_our_reference(self):
        request = mock.Mock(return_value=FakeResponse(200, {"idEnlace": 15, "urlEnlace": "https://lk.wompi.sv/xyz"}))
        self._calls(self._token_ok(), request)
        price = make_price(PlanPrice.BILLING_ANNUAL, 999)
        sub = Subscription.objects.create(user=self.user, plan=price.plan, plan_price=price, provider="wompi")

        with override_settings(WOMPI_WEBHOOK_URL="https://api.example/billing/webhooks/wompi/"):
            WompiProvider().create_checkout(
                user=self.user, plan_price=price, subscription=sub,
                success_url="https://money.wxlter.dev/pro/ok", cancel_url="https://money.wxlter.dev/pro/no",
            )

        self.assertEqual(request.call_args.args[1], "https://api.wompi.sv/EnlacePago")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["identificadorEnlaceComercio"], str(sub.checkout_reference))
        self.assertEqual(body["monto"], 9.99)
        self.assertEqual(body["configuracion"]["urlRedirect"], "https://money.wxlter.dev/pro/ok")
        self.assertIs(body["configuracion"]["esMontoEditable"], False)
        self.assertIs(body["configuracion"]["esCantidadEditable"], False)
        self.assertEqual(body["configuracion"]["urlWebhook"], "https://api.example/billing/webhooks/wompi/")
        sub.refresh_from_db()
        self.assertEqual(sub.external_subscription_id, "15")

    def test_a_response_without_url_is_an_error(self):
        self._calls(self._token_ok(), mock.Mock(return_value=FakeResponse(200, {"idEnlace": 1})))
        price = make_price()
        sub = Subscription.objects.create(user=self.user, plan=price.plan, plan_price=price, provider="wompi")
        with self.assertRaisesRegex(WompiError, "URL"):
            WompiProvider().create_checkout(
                user=self.user, plan_price=price, subscription=sub, success_url="x", cancel_url="y"
            )

    def test_cancelling_a_monthly_subscription_deactivates_its_link_and_keeps_access_until_the_end(self):
        request = mock.Mock(return_value=FakeResponse(200, {}))
        self._calls(self._token_ok(), request)
        price = make_price(PlanPrice.BILLING_MONTHLY)
        end = timezone.now() + timedelta(days=10)
        sub = Subscription.objects.create(
            user=self.user, plan=price.plan, plan_price=price, provider="wompi",
            status=Subscription.STATUS_ACTIVE, external_subscription_id="rec-123", current_period_end=end,
        )
        WompiProvider().cancel_subscription(sub)
        self.assertEqual(request.call_args.args[:2], ("POST", "https://api.wompi.sv/EnlacePagoRecurrente/rec-123"))
        sub.refresh_from_db()
        self.assertIsNotNone(sub.canceled_at)
        self.assertTrue(sub.is_in_force)

    def test_cancelling_an_annual_subscription_makes_no_call_to_wompi(self):
        request = mock.Mock()
        self._calls(self._token_ok(), request)
        price = make_price(PlanPrice.BILLING_ANNUAL)
        sub = Subscription.objects.create(
            user=self.user, plan=price.plan, plan_price=price, provider="wompi",
            status=Subscription.STATUS_ACTIVE, external_subscription_id="15",
            current_period_end=timezone.now() + timedelta(days=100),
        )
        WompiProvider().cancel_subscription(sub)
        request.assert_not_called()

    def test_if_wompi_fails_to_deactivate_the_subscription_is_not_marked_canceled(self):
        self._calls(self._token_ok(), mock.Mock(return_value=FakeResponse(500, {"e": 1})))
        price = make_price(PlanPrice.BILLING_MONTHLY)
        sub = Subscription.objects.create(
            user=self.user, plan=price.plan, plan_price=price, provider="wompi",
            status=Subscription.STATUS_ACTIVE, external_subscription_id="rec-9",
        )
        with self.assertRaises(WompiError):
            WompiProvider().cancel_subscription(sub)
        sub.refresh_from_db()
        self.assertIsNone(sub.canceled_at)


@override_settings(**CREDS)
class WompiWebhookParsingTests(TestCase):
    def _request(self, body: bytes, signature=None):
        headers = {} if signature is None else {"HTTP_WOMPI_HASH": signature}
        return RequestFactory().post(WEBHOOK, data=body, content_type="application/json", **headers)

    def test_a_valid_signature_is_accepted(self):
        body = json.dumps(approved_body("ref")).encode()
        self.assertTrue(WompiProvider().verify_webhook(self._request(body, sign(body))))

    def test_the_signature_comparison_ignores_case(self):
        body = b'{"a":1}'
        self.assertTrue(WompiProvider().verify_webhook(self._request(body, sign(body).upper())))

    def test_a_wrong_missing_or_tampered_signature_is_rejected(self):
        body = b'{"Monto":1}'
        provider = WompiProvider()
        self.assertFalse(provider.verify_webhook(self._request(body, "0" * 64)))
        self.assertFalse(provider.verify_webhook(self._request(body)))
        self.assertFalse(provider.verify_webhook(self._request(b'{"Monto":100}', sign(body))))

    def test_without_a_secret_nothing_is_valid(self):
        body = b"{}"
        with override_settings(WOMPI_CLIENT_SECRET=""):
            self.assertFalse(WompiProvider().verify_webhook(self._request(body, sign(body))))

    def test_an_approved_payment_becomes_an_activation_with_our_reference(self):
        body = json.dumps(approved_body("abc-ref", tx="tx-77")).encode()
        event = WompiProvider().parse_webhook_event(self._request(body, sign(body)))
        self.assertEqual(event.kind, "subscription.activated")
        self.assertEqual(event.reference, "abc-ref")
        self.assertEqual(event.event_id, "tx-77")
        self.assertEqual(event.external_customer_id, "ana@example.com")
        self.assertEqual(str(event.amount), "0.99")

    def test_a_declined_payment_is_ignored(self):
        body = json.dumps(approved_body("r", result="Rechazada")).encode()
        self.assertEqual(WompiProvider().parse_webhook_event(self._request(body)).kind, "payment.ignored")

    def test_a_test_payment_is_ignored_unless_test_payments_are_accepted(self):
        body = json.dumps(approved_body("r", productive=False)).encode()
        provider = WompiProvider()
        self.assertEqual(provider.parse_webhook_event(self._request(body)).kind, "payment.ignored")
        with override_settings(WOMPI_ACCEPT_TEST_PAYMENTS=True):
            self.assertEqual(provider.parse_webhook_event(self._request(body)).kind, "subscription.activated")

    def test_a_body_that_is_not_json_is_ignored_not_a_crash(self):
        self.assertEqual(WompiProvider().parse_webhook_event(self._request(b"no es json")).kind, "payment.ignored")
        self.assertEqual(WompiProvider().parse_webhook_event(self._request(b"[1,2]")).kind, "payment.ignored")


class ApplyEventTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def _sub(self, period, **kw):
        price = make_price(period)
        return Subscription.objects.create(user=self.user, plan=price.plan, plan_price=price, provider="wompi", **kw)

    def _event(self, sub, tx="tx-1"):
        from apps.billing.providers import WebhookEvent
        return WebhookEvent(kind="subscription.activated", reference=str(sub.checkout_reference), event_id=tx)

    def test_a_first_monthly_payment_activates_for_one_month(self):
        sub = self._sub(PlanPrice.BILLING_MONTHLY)
        before = timezone.now()
        apply_webhook_event(self._event(sub), provider_code="wompi")
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)
        self.assertGreater(sub.current_period_end, before + timedelta(days=27))
        self.assertLess(sub.current_period_end, before + timedelta(days=32))

    def test_a_renewal_adds_a_month_on_top_of_what_was_already_paid(self):
        end = timezone.now() + timedelta(days=10)
        sub = self._sub(PlanPrice.BILLING_MONTHLY, status=Subscription.STATUS_ACTIVE, current_period_end=end)
        apply_webhook_event(self._event(sub, "tx-2"), provider_code="wompi")
        sub.refresh_from_db()
        self.assertGreater(sub.current_period_end, end + timedelta(days=27))

    def test_an_expired_subscription_restarts_from_today_not_from_the_old_date(self):
        sub = self._sub(PlanPrice.BILLING_MONTHLY, status=Subscription.STATUS_ACTIVE,
                        current_period_end=timezone.now() - timedelta(days=90))
        apply_webhook_event(self._event(sub, "tx-3"), provider_code="wompi")
        sub.refresh_from_db()
        self.assertGreater(sub.current_period_end, timezone.now() + timedelta(days=27))

    def test_an_annual_payment_lasts_a_year_and_lifetime_never_expires(self):
        annual = self._sub(PlanPrice.BILLING_ANNUAL)
        apply_webhook_event(self._event(annual, "a"), provider_code="wompi")
        annual.refresh_from_db()
        self.assertGreater(annual.current_period_end, timezone.now() + timedelta(days=360))

        other = User.objects.create_user("bo", "bo@example.com", "pw")
        price = make_price(PlanPrice.BILLING_LIFETIME, 1999)
        life = Subscription.objects.create(user=other, plan=price.plan, plan_price=price, provider="wompi")
        apply_webhook_event(self._event(life, "b"), provider_code="wompi")
        life.refresh_from_db()
        self.assertIsNone(life.current_period_end)
        self.assertTrue(life.is_in_force)

    def test_the_same_notification_twice_is_applied_once(self):
        sub = self._sub(PlanPrice.BILLING_MONTHLY)
        apply_webhook_event(self._event(sub, "tx-dup"), provider_code="wompi")
        sub.refresh_from_db()
        first_end = sub.current_period_end
        apply_webhook_event(self._event(sub, "tx-dup"), provider_code="wompi")  # reintento de Wompi
        sub.refresh_from_db()
        self.assertEqual(sub.current_period_end, first_end)
        self.assertEqual(ProcessedWebhookEvent.objects.filter(event_id="tx-dup").count(), 1)

    def test_an_ignored_event_changes_nothing(self):
        from apps.billing.providers import WebhookEvent
        sub = self._sub(PlanPrice.BILLING_MONTHLY)
        self.assertIsNone(apply_webhook_event(WebhookEvent(kind="payment.ignored"), provider_code="wompi"))
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_PENDING)

    def test_paying_less_than_the_price_does_not_activate(self):
        from decimal import Decimal
        from apps.billing.providers import WebhookEvent
        sub = self._sub(PlanPrice.BILLING_ANNUAL)  # $9.99
        event = WebhookEvent(kind="subscription.activated", reference=str(sub.checkout_reference),
                             event_id="tx-cheap", amount=Decimal("0.01"))
        self.assertIsNone(apply_webhook_event(event, provider_code="wompi"))
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_PENDING)
        # Y el aviso no queda marcado como procesado: uno correcto posterior sí cuenta.
        self.assertFalse(ProcessedWebhookEvent.objects.filter(event_id="tx-cheap").exists())

    def test_paying_the_exact_price_or_more_activates(self):
        from decimal import Decimal
        from apps.billing.providers import WebhookEvent
        sub = self._sub(PlanPrice.BILLING_ANNUAL)
        event = WebhookEvent(kind="subscription.activated", reference=str(sub.checkout_reference),
                             event_id="tx-ok", amount=Decimal("9.99"))
        apply_webhook_event(event, provider_code="wompi")
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)

    def test_an_unknown_reference_is_a_no_op(self):
        from apps.billing.providers import WebhookEvent
        event = WebhookEvent(kind="subscription.activated", reference="00000000-0000-0000-0000-000000000000", event_id="x")
        self.assertIsNone(apply_webhook_event(event, provider_code="wompi"))
        self.assertFalse(ProcessedWebhookEvent.objects.exists())


@override_settings(**CREDS)
class WompiEndToEndTests(APITestCase):
    def setUp(self):
        providers._token_cache.update(value="", expires_at=0.0)
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        Plan.objects.create(code="free", name="Gratis", is_default=True)
        self.price = make_price(PlanPrice.BILLING_ANNUAL, 999)
        self.client.force_authenticate(self.user)

    def _patch(self, request):
        p = mock.patch.multiple(
            "apps.billing.providers.requests",
            post=mock.Mock(return_value=FakeResponse(200, {"access_token": "t", "expires_in": 3600})),
            request=request,
        )
        p.start()
        self.addCleanup(p.stop)

    def _checkout(self):
        return self.client.post("/api/v1/billing/checkout/", {
            "plan_price": self.price.id, "provider": "wompi",
            "success_url": "https://money.wxlter.dev/pro/ok", "cancel_url": "https://money.wxlter.dev/pro/no",
        }, format="json")

    def test_checkout_needs_no_pre_registered_price_and_returns_the_wompi_url(self):
        self._patch(mock.Mock(return_value=FakeResponse(200, {"idEnlace": 1, "urlEnlace": "https://lk.wompi.sv/q"})))
        resp = self._checkout()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["checkout_url"], "https://lk.wompi.sv/q")

    def test_when_wompi_is_down_the_user_gets_a_502_and_no_orphan_subscription(self):
        self._patch(mock.Mock(return_value=FakeResponse(503, {"e": 1})))
        resp = self._checkout()
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_a_signed_webhook_activates_the_subscription_end_to_end(self):
        self._patch(mock.Mock(return_value=FakeResponse(200, {"idEnlace": 1, "urlEnlace": "https://lk.wompi.sv/q"})))
        self._checkout()
        sub = Subscription.objects.get(user=self.user)
        self.client.force_authenticate(None)

        body = json.dumps(approved_body(str(sub.checkout_reference), tx="tx-e2e", amount=9.99)).encode()
        resp = self.client.post(WEBHOOK, body, content_type="application/json", HTTP_WOMPI_HASH=sign(body))

        self.assertEqual(resp.status_code, 202)
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)
        self.assertTrue(sub.is_in_force)

    def test_an_unsigned_or_wrongly_signed_webhook_is_rejected_and_changes_nothing(self):
        sub = Subscription.objects.create(user=self.user, plan=self.price.plan, plan_price=self.price, provider="wompi")
        self.client.force_authenticate(None)
        body = json.dumps(approved_body(str(sub.checkout_reference))).encode()
        for headers in ({}, {"HTTP_WOMPI_HASH": "f" * 64}):
            resp = self.client.post(WEBHOOK, body, content_type="application/json", **headers)
            self.assertEqual(resp.status_code, 403)
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_PENDING)
