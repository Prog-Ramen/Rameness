import json
import os
import tempfile
import unittest
from pathlib import Path

from rameness import config, settings
from rameness.fleet.manager import FLEET_DEFAULTS, load_fleet_cfg


class Settings(unittest.TestCase):
    """config.json and fleet.json are typed: the defaults give the schema, files are checked on load."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()

    def test_the_defaults_fit_their_own_schema(self):
        # building the schema also fails when a setting defaults to null without a declared type
        self.assertEqual(settings.problems(config.DEFAULTS, settings.config_schema()), [])
        self.assertEqual(settings.problems(FLEET_DEFAULTS, settings.fleet_schema()), [])

    def test_a_bad_file_names_every_problem(self):
        (self.tmp / "home" / "config.json").write_text(json.dumps(
            {"jev": {"backend": "kevv", "serve_gpu": "1"}, "max_turns": "50", "web": {"serch_url": "x"},
             "model_policies": {"qwen": {"thinkng": {}}}}))
        with self.assertRaises(settings.ConfigError) as e:
            config.load(self.tmp)
        msg = str(e.exception)
        self.assertIn(str(self.tmp / "home" / "config.json"), msg)
        for part in ('jev.backend: "kevv" is not one of', "jev.serve_gpu: expected integer or null, got string",
                     "max_turns: expected integer, got string", "web.serch_url: unknown setting (did you mean 'search_url'?)",
                     "model_policies.qwen.thinkng: unknown setting (did you mean 'thinking'?)"):
            self.assertIn(part, msg)

    def test_a_good_file_loads_and_may_name_its_schema(self):
        (self.tmp / "home" / "config.json").write_text(json.dumps(
            {"$schema": "./config.schema.json", "jev": {"backend": "clef", "serve_gpu": 1}, "web": {"search_url": None}}))
        cfg = config.load(self.tmp)
        self.assertEqual((cfg["jev"]["backend"], cfg["jev"]["serve_gpu"]), ("clef", 1))
        self.assertNotIn("$schema", cfg)

    def test_fleet_slots_and_environments_are_checked(self):
        (self.tmp / "home" / "fleet.json").write_text(json.dumps(
            {"slots": [{"id": "q", "kind": "vllm", "url": "http://127.0.0.1:8033", "capacity": "2"}],
             "environments": [{"kind": "ssh", "host": "me@box"}], "decision_policy": {"*": "maybe"}}))
        with self.assertRaises(settings.ConfigError) as e:
            load_fleet_cfg(self.tmp)
        msg = str(e.exception)
        for part in ('slots[0].capacity: expected integer, got string "2"', "environments[0]: missing 'id'",
                     'decision_policy.*: "maybe" is not one of'):
            self.assertIn(part, msg)

    def test_invalid_json_is_reported_with_its_file(self):
        (self.tmp / "home" / "config.json").write_text("{\"provider\": ")
        with self.assertRaises(settings.ConfigError) as e:
            config.load(self.tmp)
        self.assertIn("not valid JSON", str(e.exception))


class AnthropicProviderSetup(unittest.TestCase):
    def test_the_provider_keeps_its_fallback_setting_and_requires_credentials(self):
        from rameness.llm import AnthropicProvider
        saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            os.environ["ANTHROPIC_API_KEY"] = "test-key"
            self.assertFalse(AnthropicProvider("claude-x", fallbacks=False).fallbacks)
            del os.environ["ANTHROPIC_API_KEY"]
            with self.assertRaises(RuntimeError):
                AnthropicProvider("claude-x")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
