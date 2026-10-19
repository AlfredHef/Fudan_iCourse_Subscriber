import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.check_model_providers import check_models


class ModelProbeTests(unittest.TestCase):
    providers = [{"name": "example", "api_key": "test", "base_url": "https://example.invalid/v1", "models": ["retired", "available"]}]

    def test_rejected_model_falls_back_to_accepted_model(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [ValueError("Model has no provider supported"), SimpleNamespace(choices=[object()])]
        self.assertEqual(check_models(self.providers, lambda **kwargs: client), "example/available")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_all_rejected_models_stop_the_job(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = ValueError("Model has no provider supported")
        with self.assertRaises(RuntimeError):
            check_models(self.providers, lambda **kwargs: client)

    def test_missing_credentials_cannot_silently_pass(self):
        with self.assertRaises(RuntimeError):
            check_models([], MagicMock())


if __name__ == "__main__":
    unittest.main()
