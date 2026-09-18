"""
Cliente de Gemini (`apps/ai/client.py`). Nunca sale a la red: lo que importa
probar es cómo se arma el request y, sobre todo, **que ningún fallo de Google
se propague como algo distinto de `AIUnavailable`** — de eso depende que la
app siga andando con la IA caída.
"""
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from apps.ai import client

_POST = "apps.ai.client.requests.post"

_OK_BODY = {
    "candidates": [{"content": {"parts": [{"text": '{"amount": 12.5}'}]}}],
    "usageMetadata": {"promptTokenCount": 630, "candidatesTokenCount": 120},
}


def _response(status_code=200, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = body if body is not None else _OK_BODY
    return resp


@override_settings(GEMINI_API_KEY="k-de-prueba", AI_TIMEOUT_SECONDS=25)
class RequestTests(SimpleTestCase):
    def test_manda_la_key_por_header_y_no_en_la_url(self):
        """Las URLs quedan en el log de acceso de cualquier proxy del camino."""
        with patch(_POST, return_value=_response()) as post:
            client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertNotIn("k-de-prueba", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "k-de-prueba")
        self.assertEqual(kwargs["timeout"], 25)

    def test_el_esquema_de_respuesta_pide_json_estructurado(self):
        schema = {"type": "object", "properties": {"amount": {"type": "number"}}}
        with patch(_POST, return_value=_response()) as post:
            client.generate(
                model="gemini-2.5-flash", parts=[{"text": "hola"}], response_schema=schema
            )
        config = post.call_args[1]["json"]["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseSchema"], schema)
        # Extracción, no redacción: del mismo recibo queremos siempre el mismo
        # monto.
        self.assertEqual(config["temperature"], 0.0)

    def test_devuelve_el_texto_y_los_tokens_para_el_registro(self):
        with patch(_POST, return_value=_response()):
            resp = client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(resp.json(), {"amount": 12.5})
        self.assertEqual((resp.input_tokens, resp.output_tokens), (630, 120))


@override_settings(GEMINI_API_KEY="k-de-prueba")
class FallosTests(SimpleTestCase):
    def test_sin_key_no_sale_a_la_red(self):
        with override_settings(GEMINI_API_KEY=""):
            with patch(_POST) as post:
                with self.assertRaises(client.AIUnavailable) as ctx:
                    client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
            post.assert_not_called()
        self.assertEqual(ctx.exception.code, "no_api_key")

    def test_un_timeout_es_ai_unavailable_y_no_una_excepcion_de_requests(self):
        with patch(_POST, side_effect=requests.Timeout):
            with self.assertRaises(client.AIUnavailable) as ctx:
                client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(ctx.exception.code, "timeout")

    def test_un_400_no_se_reintenta_porque_va_a_fallar_igual(self):
        with patch(_POST, return_value=_response(400, text="bad request")) as post:
            with self.assertRaises(client.AIUnavailable) as ctx:
                client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(ctx.exception.code, "http_400")

    @patch("apps.ai.client.time.sleep")
    def test_un_503_se_reintenta_una_sola_vez(self, _sleep):
        with patch(_POST, return_value=_response(503, text="unavailable")) as post:
            with self.assertRaises(client.AIUnavailable):
                client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(post.call_count, 2)

    @patch("apps.ai.client.time.sleep")
    def test_si_el_reintento_sale_bien_la_llamada_sale_bien(self, _sleep):
        with patch(_POST, side_effect=[_response(503, text="x"), _response()]):
            resp = client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(resp.json(), {"amount": 12.5})

    def test_el_cuerpo_del_error_de_google_no_llega_al_codigo_de_error(self):
        """Puede traer el prompt de vuelta; va al log del servidor, nada más."""
        with patch(_POST, return_value=_response(400, text="tu recibo decía 12.50 en Super Selectos")):
            with self.assertRaises(client.AIUnavailable) as ctx:
                client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertNotIn("Selectos", ctx.exception.code)
        self.assertNotIn("Selectos", str(ctx.exception))

    def test_una_respuesta_que_no_es_json_no_revienta_el_llamador(self):
        body = {"candidates": [{"content": {"parts": [{"text": "no soy json"}]}}]}
        with patch(_POST, return_value=_response(body=body)):
            resp = client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
            with self.assertRaises(client.AIUnavailable) as ctx:
                resp.json()
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_una_respuesta_bloqueada_por_filtros_tambien_es_ai_unavailable(self):
        body = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        with patch(_POST, return_value=_response(body=body)):
            with self.assertRaises(client.AIUnavailable) as ctx:
                client.generate(model="gemini-2.5-flash", parts=[{"text": "hola"}])
        self.assertEqual(ctx.exception.code, "no_candidates_SAFETY")
