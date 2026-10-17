from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hotel_search.cli import load_env, main, write_result
from hotel_search.providers import ProviderError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "hotels.json"
        self.env_file = self.directory / "missing.env"
        self.trip = ["--provider", "serpapi", "--city", "Kingston", "--check-in", "2028-01-01", "--check-out", "2028-01-03"]

    def invoke(self, arguments, environment=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment or {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--env-file", str(self.env_file), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_demo_stdout_is_one_json_document_events_go_to_stderr(self):
        with patch("hotel_search.providers.urlopen", side_effect=AssertionError("unexpected network")):
            code, stdout, stderr = self.invoke(["--demo", "--output", "-", "--events"])
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        self.assertEqual(result["mode"], "fixture")
        self.assertEqual(result["shortlist"], [])
        self.assertTrue(result["hotels"])
        events = [json.loads(line) for line in stderr.splitlines()]
        self.assertTrue(events)
        self.assertEqual(events[-1]["step"], "result")

    def test_missing_key_replaces_stale_success_with_error_json(self):
        self.output.write_text('{"status":"success","shortlist":["stale"]}', encoding="utf-8")
        with patch("hotel_search.cli.SerpApiProvider") as provider:
            code, stdout, stderr = self.invoke([*self.trip, "--output", str(self.output)])
        provider.assert_not_called()
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(result["hotels"], [])
        self.assertEqual(result["errors"][0]["code"], "configuration_error")
        self.assertIn("configuration_error", stderr)

    def test_provider_failure_is_error_json_without_secret(self):
        with patch("hotel_search.cli.SerpApiProvider", side_effect=ProviderError("Authentication failed", "authentication_error")):
            code, stdout, stderr = self.invoke([*self.trip, "--output", "-"], {"SERPAPI_API_KEY": "PRIVATE_SECRET"})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["errors"][0]["code"], "authentication_error")
        self.assertNotIn("PRIVATE_SECRET", stdout + stderr)

    def test_json_request_from_stdin_preserves_occupancy(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "examples/search.fixture.json").read_text())
        with patch("sys.stdin", io.StringIO(json.dumps(fixture["request"]))):
            code, stdout, _ = self.invoke(["--demo", "--request", "-", "--output", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["request"]["guests"], fixture["request"]["guests"])

    def test_invalid_request_and_mixed_inputs_write_error_document(self):
        request_path = self.directory / "request.json"
        request_path.write_text('{"city":"Kingston"}', encoding="utf-8")
        for arguments in [["--request", str(request_path)], ["--request", str(request_path), *self.trip]]:
            with self.subTest(arguments=arguments):
                code, stdout, _ = self.invoke([*arguments, "--output", "-"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stdout)["errors"][0]["code"], "invalid_input")

    def test_fixture_trip_mismatch_fails_without_network(self):
        with patch("hotel_search.providers.urlopen", side_effect=AssertionError("unexpected network")):
            code, stdout, _ = self.invoke(["--demo", *self.trip, "--output", "-"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["status"], "error")

    def test_invalid_limits_fail_before_provider_construction(self):
        with patch("hotel_search.cli.SerpApiProvider") as provider:
            code, stdout, _ = self.invoke([*self.trip, "--workers", "0", "--output", "-"])
        provider.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["errors"][0]["code"], "invalid_input")

    def test_atomic_output_is_fully_serialized_before_replacing_existing_file(self):
        self.output.write_text("old result", encoding="utf-8")
        expected = {"status": "success", "name": "Hôtel", "hotels": []}
        real_replace = os.replace
        observed = []

        def check_then_replace(source, destination):
            observed.append((Path(source), Path(destination)))
            self.assertEqual(self.output.read_text(), "old result")
            self.assertEqual(Path(source).parent, self.output.parent)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), expected)
            real_replace(source, destination)

        with patch("hotel_search.cli.os.replace", side_effect=check_then_replace):
            write_result(expected, self.output)
        self.assertEqual(len(observed), 1)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), expected)
        self.assertFalse(observed[0][0].exists())
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_replace_failure_preserves_previous_file_and_cleans_temporary_file(self):
        self.output.write_text("old result", encoding="utf-8")
        with patch("hotel_search.cli.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                write_result({"status": "success"}, self.output)
        self.assertEqual(self.output.read_text(), "old result")
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_nonfinite_json_is_rejected_before_touching_existing_file(self):
        self.output.write_text("old result", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_result({"price": float("nan")}, self.output)
        self.assertEqual(self.output.read_text(), "old result")

    def test_env_file_is_literal_and_never_overrides_existing_key(self):
        self.env_file.write_text("OTHER_SECRET=ignored\nexport SERPAPI_API_KEY='$(not-executed)'\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            load_env(self.env_file)
            self.assertEqual(os.environ["SERPAPI_API_KEY"], "$(not-executed)")
            self.assertNotIn("OTHER_SECRET", os.environ)
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "existing"}, clear=True):
            load_env(self.env_file)
            self.assertEqual(os.environ["SERPAPI_API_KEY"], "existing")


if __name__ == "__main__":
    unittest.main()
