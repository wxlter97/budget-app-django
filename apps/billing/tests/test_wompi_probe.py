"""`manage.py wompi_probe --diagnostico-red`: requests vs curl, para distinguir un
bloqueo por IP de uno por la huella TLS del cliente HTTP."""
import subprocess
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.billing.management.commands.wompi_probe import curl_post_form

CREDS = dict(WOMPI_CLIENT_ID="app-id", WOMPI_CLIENT_SECRET="secreto")


class CurlPostFormTests(TestCase):
    def test_parses_the_status_code_and_body_from_curl_output(self):
        proc = mock.Mock(returncode=0, stdout='{"access_token":"x"}\n200', stderr="")
        with mock.patch("subprocess.run", return_value=proc) as run:
            code, body = curl_post_form("https://id.wompi.sv/connect/token", {"a": "1"})
        self.assertEqual(code, "200")
        self.assertEqual(body, '{"access_token":"x"}')
        # El cuerpo va por stdin, no en los argumentos -- no debe verse en la lista de procesos.
        self.assertEqual(run.call_args.kwargs["input"], "a=1")
        self.assertNotIn("a=1", run.call_args.args[0])

    def test_a_multiline_body_is_still_split_correctly_from_the_status_code(self):
        proc = mock.Mock(returncode=0, stdout="<html>\n<body>403</body>\n</html>\n403", stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            code, body = curl_post_form("https://x", {})
        self.assertEqual(code, "403")
        self.assertIn("<body>403</body>", body)

    def test_when_curl_is_not_installed_it_does_not_crash(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            code, body = curl_post_form("https://x", {})
        self.assertIsNone(code)
        self.assertIn("curl", body)

    def test_a_timeout_does_not_crash(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 15)):
            code, body = curl_post_form("https://x", {})
        self.assertIsNone(code)


@override_settings(**CREDS)
class DiagnosticoRedCommandTests(TestCase):
    def _run(self, requests_response, curl_result):
        out = StringIO()
        with mock.patch("requests.post", return_value=requests_response), \
             mock.patch(
                 "apps.billing.management.commands.wompi_probe.curl_post_form",
                 return_value=curl_result,
             ):
            call_command("wompi_probe", "--diagnostico-red", stdout=out)
        return out.getvalue()

    def test_both_clients_blocked_points_at_the_ip_not_the_client(self):
        blocked = mock.Mock(status_code=403, text="<center>Microsoft-Azure-Application-Gateway/v2</center>")
        out = self._run(blocked, ("403", "Microsoft-Azure-Application-Gateway"))
        self.assertIn("no es la huella del cliente HTTP", out)

    def test_only_requests_blocked_points_at_the_client_fingerprint(self):
        blocked = mock.Mock(status_code=403, text="Microsoft-Azure-Application-Gateway")
        out = self._run(blocked, ("400", '{"error":"invalid_request"}'))
        self.assertIn("huella TLS del cliente Python", out)

    def test_neither_blocked(self):
        ok = mock.Mock(status_code=200, text='{"access_token":"x"}')
        out = self._run(ok, ("200", '{"access_token":"x"}'))
        self.assertIn("Ninguno fue bloqueado", out)

    @override_settings(WOMPI_PROXY_URL="http://user:pass@relay.example:3128")
    def test_also_tries_the_configured_relay_and_reports_if_it_gets_through(self):
        ok_direct = mock.Mock(status_code=403, text="Microsoft-Azure-Application-Gateway")
        ok_relay = mock.Mock(status_code=200, text='{"access_token":"x"}')
        out = StringIO()
        with mock.patch("requests.post", side_effect=[ok_direct, ok_relay]), \
             mock.patch(
                 "apps.billing.management.commands.wompi_probe.curl_post_form",
                 return_value=("403", "Microsoft-Azure-Application-Gateway"),
             ):
            call_command("wompi_probe", "--diagnostico-red", stdout=out)
        text = out.getvalue()
        self.assertIn("requests vía WOMPI_PROXY_URL: 200", text)
        self.assertIn("El relay (WOMPI_PROXY_URL) SÍ pasa", text)

    @override_settings(WOMPI_PROXY_URL="http://user:pass@relay.example:3128")
    def test_reports_clearly_when_the_relay_is_also_blocked(self):
        blocked = mock.Mock(status_code=403, text="Microsoft-Azure-Application-Gateway")
        out = StringIO()
        with mock.patch("requests.post", return_value=blocked), \
             mock.patch(
                 "apps.billing.management.commands.wompi_probe.curl_post_form",
                 return_value=("403", "Microsoft-Azure-Application-Gateway"),
             ):
            call_command("wompi_probe", "--diagnostico-red", stdout=out)
        self.assertIn("también fue bloqueado", out.getvalue())

    def test_without_credentials_it_refuses_clearly(self):
        out = StringIO()
        with override_settings(WOMPI_CLIENT_ID="", WOMPI_CLIENT_SECRET=""):
            with self.assertRaisesMessage(Exception, "WOMPI_CLIENT_ID"):
                call_command("wompi_probe", "--diagnostico-red", stdout=out)
