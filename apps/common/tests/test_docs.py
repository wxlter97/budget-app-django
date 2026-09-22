"""Docs técnicas privadas (`/docs/`) -- staff-only, contenido leído de
Markdown en `docs/reference/` y `CHANGELOG.md` (ver `apps.common.docs_views`).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class DocsAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
        cls.regular = User.objects.create_user("bob", "bob@example.com", "pw")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/docs/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_non_staff_is_redirected_to_login(self):
        self.client.force_login(self.regular)
        resp = self.client.get("/docs/")
        self.assertEqual(resp.status_code, 302)

    def test_staff_sees_the_index(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/docs/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Documentación técnica")

    def test_staff_sees_the_changelog(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/docs/changelog/")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_slug_is_404(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/docs/no-existe/")
        self.assertEqual(resp.status_code, 404)

    def test_slug_with_path_traversal_chars_is_404_not_500(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/docs/..%2f..%2fetc%2fpasswd/")
        self.assertIn(resp.status_code, (400, 404))
