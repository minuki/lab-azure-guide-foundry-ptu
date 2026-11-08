from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

import client_spillover as sample


class ClientSpilloverTests(unittest.TestCase):
    def run_case(self, base_result: int | Exception, spillover_status: int = 200, base_code: str | None = None):
        self.calls: list[httpx.Request] = []
        self.payloads: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            self.payloads.append(request.read())
            if request.url.host == "base.example":
                if isinstance(base_result, Exception):
                    raise base_result
                status = base_result
                code = base_code
            else:
                status = spillover_status
                code = "rate_limit_exceeded"
            if status == 200:
                return httpx.Response(200, json={
                    "created": 0, "data": [{"b64_json": base64.b64encode(b"test-output").decode()}],
                })
            return httpx.Response(status, headers={"retry-after-ms": "424"}, json={
                "error": {
                    "message": "Rate limit exceeded.",
                    "type": "invalid_request_error" if code is None else "too_many_requests",
                    "param": None, "code": code,
                },
            })

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "input.jpg"
            image.write_bytes(b"complete-upload-marker")
            with OpenAI(
                base_url="https://base.example/openai/v1/", api_key="base-test-key",
                max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as base_client, OpenAI(
                base_url="https://spillover.example/openai/v1/", api_key="spillover-test-key",
                max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as spillover_client:
                return sample.edit_with_spillover(
                    base_client, spillover_client, image, "test-prompt", "base-model", "spillover-model",
                )

    def test_base_success_never_calls_spillover(self) -> None:
        response, route = self.run_case(200)
        self.assertEqual(route, "base")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(response.data[0].b64_json, base64.b64encode(b"test-output").decode())

    def test_both_observed_429_error_shapes_route_once_and_reopen_image(self) -> None:
        for code in (None, "rate_limit_exceeded"):
            with self.subTest(code=code), self.assertLogs(sample.LOG, level="INFO") as logs:
                response, route = self.run_case(429, base_code=code)
            self.assertEqual(route, "spillover")
            self.assertEqual(len(self.calls), 2)
            self.assertEqual([request.url.host for request in self.calls], ["base.example", "spillover.example"])
            self.assertEqual([request.url.path for request in self.calls], ["/openai/v1/images/edits"] * 2)
            self.assertEqual([request.headers["authorization"] for request in self.calls], ["Bearer base-test-key", "Bearer spillover-test-key"])
            for payload in self.payloads:
                self.assertIn(b"complete-upload-marker", payload)
                self.assertIn(b"test-prompt", payload)
                self.assertIn(b"1152x864", payload)
            self.assertIn(b"base-model", self.payloads[0])
            self.assertIn(b"spillover-model", self.payloads[1])
            self.assertTrue(response.data)
            self.assertTrue(any("client_spillover base_HTTP=429" in line for line in logs.output))
            self.assertFalse(any("test-key" in line for line in logs.output))

    def test_non_429_errors_do_not_spillover(self) -> None:
        for status in (400, 401, 403, 404, 500, 503):
            with self.subTest(status=status), self.assertRaises(APIStatusError) as error:
                self.run_case(status)
            self.assertEqual(error.exception.status_code, status)
            self.assertEqual(len(self.calls), 1)

    def test_transport_error_and_timeout_do_not_spillover(self) -> None:
        for error in (httpx.ConnectError("test connection failure"), httpx.ReadTimeout("test timeout")):
            with self.subTest(error=error), self.assertRaises(APIConnectionError):
                self.run_case(error)
            self.assertEqual(len(self.calls), 1)

    def test_spillover_failure_is_propagated_without_a_third_attempt(self) -> None:
        for status in (429, 500):
            with self.subTest(status=status), self.assertRaises(APIStatusError) as error:
                self.run_case(429, spillover_status=status)
            self.assertEqual(error.exception.status_code, status)
            self.assertEqual(len(self.calls), 2)

    def test_endpoint_validation(self) -> None:
        self.assertEqual(sample.validate_endpoint("https://base.example/openai/v1"), "https://base.example/openai/v1/")
        for endpoint in (
            sample.BASE_ENDPOINT, "http://base.example/openai/v1/",
            "https://base.example/", "https://key@base.example/openai/v1/",
            "https://base.example/openai/v1/?key=secret",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                sample.validate_endpoint(endpoint)

    def test_cli_requires_explicit_inputs_and_accepts_existing_files(self) -> None:
        arguments = [
            "client_spillover.py",
            "--base-endpoint", "https://base.example/openai/v1/",
            "--spillover-endpoint", "https://spillover.example/openai/v1/",
        ]
        with patch("sys.argv", arguments), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                sample.parse_args()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("--image", stderr.getvalue())
            self.assertIn("--prompt", stderr.getvalue())
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "input.jpg"
            prompt = Path(temporary) / "prompt.txt"
            image.write_bytes(b"fixture")
            prompt.write_text("test prompt", encoding="utf-8")
            with patch("sys.argv", arguments + ["--image", str(image), "--prompt", str(prompt)]):
                parsed = sample.parse_args()
            self.assertEqual(parsed.image, image)
            self.assertEqual(parsed.prompt, prompt)


if __name__ == "__main__":
    unittest.main()
