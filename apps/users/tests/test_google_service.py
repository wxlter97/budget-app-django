"""Unit tests de `verify_google_id_token` -- mockea la llamada HTTP a
Google (`urllib.request.urlopen`); el flujo end-to-end del endpoint está en
test_google_auth.py."""
import json
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import TestCase, override_settings

from apps.users.services import GoogleTokenError, verify_google_id_token


class VerifyGoogleIdTokenTests(TestCase):
    def _claims(self, **overrides):
        claims = {
            "email": "a@example.com",
            "email_verified": "true",
            "aud": "client-id",
        }
        claims.update(overrides)
        return claims

    @patch("apps.users.services.urllib.request.urlopen")
    def test_valid_token_returns_claims(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            self._claims()
        ).encode()
        claims = verify_google_id_token("sometoken")
        self.assertEqual(claims["email"], "a@example.com")

    def test_missing_token_raises(self):
        with self.assertRaises(GoogleTokenError):
            verify_google_id_token("")

    @patch("apps.users.services.urllib.request.urlopen")
    def test_http_error_raises_google_token_error(self, urlopen):
        urlopen.side_effect = HTTPError("url", 400, "Bad Request", {}, None)
        with self.assertRaises(GoogleTokenError):
            verify_google_id_token("sometoken")

    @patch("apps.users.services.urllib.request.urlopen")
    def test_network_error_raises_google_token_error(self, urlopen):
        urlopen.side_effect = URLError("no network")
        with self.assertRaises(GoogleTokenError):
            verify_google_id_token("sometoken")

    @patch("apps.users.services.urllib.request.urlopen")
    def test_unverified_email_is_rejected(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            self._claims(email_verified="false")
        ).encode()
        with self.assertRaises(GoogleTokenError):
            verify_google_id_token("sometoken")

    @patch("apps.users.services.urllib.request.urlopen")
    @override_settings(GOOGLE_CLIENT_IDS=["expected-client-id"])
    def test_wrong_audience_is_rejected(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            self._claims(aud="other-client-id")
        ).encode()
        with self.assertRaises(GoogleTokenError):
            verify_google_id_token("sometoken")

    @patch("apps.users.services.urllib.request.urlopen")
    @override_settings(GOOGLE_CLIENT_IDS=["expected-client-id"])
    def test_matching_audience_is_accepted(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            self._claims(aud="expected-client-id")
        ).encode()
        claims = verify_google_id_token("sometoken")
        self.assertEqual(claims["aud"], "expected-client-id")
