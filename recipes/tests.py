from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import SavedRecipe


class _MockInteraction:
    def __init__(self, text):
        self.output_text = text


VALID_JSON = '{"title": "Test Dish", "ingredients": ["potato"], "steps": ["boil"]}'


def _make_client_with_responses(responses):
    """Patch target for genai.Client: returns object whose .interactions.create yields responses."""
    state = {"i": 0}
    def create(model, input):
        i = state["i"]
        state["i"] += 1
        if i >= len(responses):
            raise RuntimeError("no more mocked responses")
        r = responses[i]
        if isinstance(r, Exception):
            raise r
        return _MockInteraction(r)
    fake = type("FakeClient", (), {})()
    fake.interactions = type("FakeInteractions", (), {"create": staticmethod(create)})()
    return fake


class SmartChefUndoTests(TestCase):
    def setUp(self):
        self.index_url = reverse("recipes:index")
        self.suggest_accept_url = reverse("recipes:accept_pending")
        self.suggest_reject_url = reverse("recipes:reject_pending")
        self.undo_url = reverse("recipes:undo_last")
        self.env_patcher = patch("recipes.views.dotenv_values")
        self.env_mock = self.env_patcher.start()
        self.env_mock.return_value = {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"}
        self.addCleanup(self.env_patcher.stop)

    def _patch_client(self, responses):
        p = patch("recipes.views.genai.Client")
        m = p.start()
        m.return_value = _make_client_with_responses(responses)
        self.addCleanup(p.stop)

    def _generate_initial(self):
        self._patch_client([VALID_JSON])
        resp = self.client.post(self.index_url, {
            "action": "generate",
            "products": "potato",
            "minutes": "30",
            "language": "ru",
        })
        self.assertEqual(resp.status_code, 200)
        return self.client.session["smartchef_draft"]

    def _propose(self, base_id=""):
        self._patch_client([VALID_JSON])
        resp = self.client.post(self.index_url, {
            "action": "suggest",
            "prompt": "make it vegan",
            "language": "ru",
            "base_recipe_id": base_id,
        })
        self.assertEqual(resp.status_code, 200)
        return self.client.session["smartchef_pending"]

    # ── 1. propose creates pending ──
    def test_propose_creates_pending(self):
        pending = self._propose()
        self.assertIn("smartchef_pending", self.client.session)
        self.assertEqual(pending["candidate"]["title"], "Test Dish")
        self.assertTrue(pending["token"])
        self.assertTrue(pending["pending_id"])
        self.assertGreater(pending["expires_at"], pending["created_at"])

    # ── 2. reject clears pending ──
    def test_reject_clears_pending(self):
        pending = self._propose()
        resp = self.client.post(self.suggest_reject_url, {
            "pending_id": pending["pending_id"],
            "token": pending["token"],
            "language": "ru",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("smartchef_pending", self.client.session)

    # ── 3. provider error (empty response) ──
    def test_propose_error_on_empty_response(self):
        self._patch_client([""])
        resp = self.client.post(self.index_url, {
            "action": "suggest",
            "prompt": "abc",
            "language": "ru",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("smartchef_pending", self.client.session)
        self.assertTrue(resp.context["error"])

    # ── 4. accept restores recipe; undo restores snapshot ──
    def test_accept_and_undo(self):
        old_draft = self._generate_initial()
        old_recipe = old_draft["recipe"]
        old_token = old_draft["token"]

        pending = self._propose(base_id=old_token)
        resp = self.client.post(self.suggest_accept_url, {
            "pending_id": pending["pending_id"],
            "token": pending["token"],
            "language": "ru",
        })
        self.assertEqual(resp.status_code, 302)

        new_draft = self.client.session["smartchef_draft"]
        self.assertIn("Test Dish", new_draft["recipe"])
        self.assertEqual(new_draft["token"], old_token)

        undo = self.client.session["smartchef_undo"]
        self.assertEqual(undo["action"], "accept_pending")
        self.assertEqual(undo["recipe_id"], old_token)
        self.assertEqual(undo["old_draft"]["recipe"], old_recipe)
        self.assertEqual(undo["after"], new_draft["recipe"])

        resp = self.client.post(self.undo_url, {"language": "ru"})
        self.assertEqual(resp.status_code, 302)

        restored = self.client.session["smartchef_draft"]
        self.assertEqual(restored["recipe"], old_recipe)
        self.assertEqual(restored["token"], old_token)
        self.assertNotIn("smartchef_undo", self.client.session)
        self.assertNotIn("smartchef_pending", self.client.session)

    # ── 5. exactly one undo — second undo is a no-op ──
    def test_second_undo_noop(self):
        old_draft = self._generate_initial()
        old_recipe = old_draft["recipe"]

        pending = self._propose(base_id=old_draft["token"])
        self.client.post(self.suggest_accept_url, {
            "pending_id": pending["pending_id"],
            "token": pending["token"],
            "language": "ru",
        })

        self.client.post(self.undo_url, {"language": "ru"})
        after_first = self.client.session["smartchef_draft"]
        self.assertEqual(after_first["recipe"], old_recipe)

        resp = self.client.post(self.undo_url, {"language": "ru"})
        self.assertEqual(resp.status_code, 302)
        after_second = self.client.session["smartchef_draft"]
        self.assertEqual(after_second["recipe"], old_recipe)
        self.assertNotIn("smartchef_undo", self.client.session)

    # ── 6. conditional: stale recipe → no overwrite ──
    def test_undo_skips_when_stale(self):
        old_draft = self._generate_initial()
        pending = self._propose(base_id=old_draft["token"])
        self.client.post(self.suggest_accept_url, {
            "pending_id": pending["pending_id"],
            "token": pending["token"],
            "language": "ru",
        })

        # Simulate external mutation
        session = self.client.session
        draft = session["smartchef_draft"]
        draft["recipe"] = "modified externally"
        session["smartchef_draft"] = draft
        session.save()

        self.client.post(self.undo_url, {"language": "ru"})
        current = self.client.session["smartchef_draft"]
        self.assertEqual(current["recipe"], "modified externally")
        self.assertNotIn("smartchef_undo", self.client.session)