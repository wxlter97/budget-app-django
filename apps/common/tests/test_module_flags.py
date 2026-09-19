"""Interruptor manual por módulo (ver `models.ModuleFlag` / `services.py`):
apagar uno desde el admin bloquea el endpoint real con 503, y el endpoint de
estado le avisa al cliente sin que tenga que tocar el botón primero."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.models import ModuleFlag
from apps.common.services import ModuleDisabled, disabled_modules, module_enabled, require_module_enabled

User = get_user_model()


class ModuleFlagServiceTests(APITestCase):
    def test_unknown_key_is_enabled(self):
        self.assertTrue(module_enabled("no-existe-todavia"))
        require_module_enabled("no-existe-todavia")  # no levanta

    def test_disabled_flag_blocks(self):
        ModuleFlag.objects.create(key="algo", label="Algo", is_enabled=False)
        self.assertFalse(module_enabled("algo"))
        with self.assertRaises(ModuleDisabled):
            require_module_enabled("algo")

    def test_enabled_flag_passes(self):
        ModuleFlag.objects.create(key="algo", label="Algo", is_enabled=True)
        self.assertTrue(module_enabled("algo"))
        require_module_enabled("algo")

    def test_disabled_message_used_when_set(self):
        ModuleFlag.objects.create(
            key="algo", label="Algo", is_enabled=False, disabled_message="Volvé mañana.",
        )
        with self.assertRaises(ModuleDisabled) as ctx:
            require_module_enabled("algo")
        self.assertEqual(str(ctx.exception.detail), "Volvé mañana.")

    def test_disabled_modules_lists_only_the_off_ones(self):
        ModuleFlag.objects.create(key="off1", label="Off 1", is_enabled=False, disabled_message="msg1")
        ModuleFlag.objects.create(key="on1", label="On 1", is_enabled=True)
        self.assertEqual(disabled_modules(), {"off1": "msg1"})


class ModuleFlagsViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.client.force_authenticate(self.user)

    def test_lists_only_disabled_modules(self):
        # `update_or_create` porque `common.0002_seed_module_flags` ya
        # sembró "ai"/"excel_import" (habilitados) -- ver esa migración.
        ModuleFlag.objects.update_or_create(
            key="ai", defaults={"label": "IA", "is_enabled": False, "disabled_message": "Sin IA por ahora."}
        )
        ModuleFlag.objects.update_or_create(
            key="excel_import", defaults={"label": "Excel", "is_enabled": True}
        )
        resp = self.client.get("/api/v1/module-flags/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["disabled"], {"ai": "Sin IA por ahora."})

    def test_requires_auth(self):
        self.client.force_authenticate(None)
        resp = self.client.get("/api/v1/module-flags/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ModuleFlagAdminListEditableTests(APITestCase):
    """`list_editable` en el admin es el punto entero del modelo -- confirmá
    que el campo siga registrado ahí y no se pierda en un refactor."""

    def test_is_enabled_is_list_editable(self):
        from apps.common.admin import ModuleFlagAdmin

        self.assertIn("is_enabled", ModuleFlagAdmin.list_editable)
