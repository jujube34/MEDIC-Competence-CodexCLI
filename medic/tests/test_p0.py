import json
import unittest

import httpx
import jsonschema

from medic.p0.appserver import validate
from medic.p0.common import REVISION
from medic.p0.echo_mcp import handle
from medic.p0.gate import evaluate
from medic.p0.provider import Config, Provider, ProbeError, parse_sse, response_text, tool_result


class ProviderContracts(unittest.TestCase):
    def test_unknown_tool_and_injection_argument_rejected(self):
        self.assertEqual(tool_result("shell", "{}", "r"), {"error": "unknown_tool"})
        self.assertEqual(tool_result("echo_evidence_id", '{"evidence_id":"synthetic-evidence-001","path":"../employee"}', "r"),
                         {"error": "invalid_arguments"})
        self.assertEqual(tool_result("echo_evidence_id", '{"evidence_id":"private-employee"}', "r"),
                         {"error": "unknown_evidence"})

    def test_malformed_arguments(self):
        for args in ("{", "null", "[]", '{"evidence_id":1}'):
            self.assertEqual(tool_result("echo_evidence_id", args, "r"), {"error": "invalid_arguments"})

    def test_receipt_is_from_tool_output(self):
        config = Config("https://example.invalid/v1", "secret-token")
        count = 0
        def responder(request):
            nonlocal count
            count += 1
            body = json.loads(request.content)
            if count == 1:
                output = [{"type": "function_call", "call_id": "call1", "name": "echo_evidence_id",
                           "arguments": '{"evidence_id":"synthetic-evidence-001"}'}]
            else:
                self.assertEqual(body["input"][-1]["call_id"], "call1")
                receipt = json.loads(body["input"][-1]["output"])["receipt"]
                output = [{"type": "message", "content": [{"type": "output_text", "text": receipt}]}]
            return httpx.Response(200, json={"status": "completed", "output": output})
        provider = Provider(config, httpx.MockTransport(responder))
        try:
            self.assertTrue(provider.tool_loop()["receipt_verified"])
            self.assertNotIn(config.token, json.dumps(provider.trace))
            self.assertNotIn("receipt-", json.dumps(provider.trace))
        finally:
            provider.close()

    def test_tool_loop_is_bounded(self):
        count = 0
        def responder(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "function_call",
                "call_id": str(count), "name": "echo_evidence_id", "arguments": "{}"}]})
        provider = Provider(Config("https://example.invalid", "x"), httpx.MockTransport(responder))
        try:
            with self.assertRaises(ProbeError):
                provider.tool_loop()
            self.assertLessEqual(count, 4)
        finally:
            provider.close()

    def test_auth_and_redirects_are_not_retried(self):
        for status in (301, 401, 403, 429, 500):
            count = 0
            def responder(request):
                nonlocal count
                count += 1
                return httpx.Response(status, headers={"location": "https://other.invalid"}, text="secret-token")
            p = Provider(Config("https://example.invalid", "secret-token"), httpx.MockTransport(responder))
            try:
                with self.assertRaisesRegex(ProbeError, f"http_{status}"):
                    p.responses("synthetic")
                self.assertEqual(count, 1)
                self.assertNotIn("secret-token", json.dumps(p.trace))
            finally:
                p.close()

    def test_request_budget(self):
        p = Provider(Config("https://example.invalid", "x"), max_requests=0)
        try:
            with self.assertRaisesRegex(ProbeError, "budget"):
                p.responses("synthetic")
        finally:
            p.close()

    def test_sse_multiline_and_interrupted_frame(self):
        self.assertEqual(list(parse_sse([": heartbeat", "event: response.created", "data: {", 'data: "a":1}', ""])),
                         [("response.created", '{\n"a":1}')])
        with self.assertRaisesRegex(ProbeError, "unterminated"):
            list(parse_sse(['data: {"type":"response.completed"}']))

    def test_failed_and_length_truncated_are_not_success(self):
        for value in ({"status": "incomplete"}, {"status": "failed"}, {"status": "completed", "error": {"code": "x"}},
                      {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}}):
            with self.assertRaises(ProbeError):
                response_text(value)

    def test_sse_missing_terminal_is_rejected(self):
        content = 'event: response.created\ndata: {"type":"response.created"}\n\n'
        p = Provider(Config("https://example.invalid", "x"), httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=content)))
        try:
            with self.assertRaisesRegex(ProbeError, "lifecycle"):
                p.sse("responses")
        finally:
            p.close()

    def test_url_credentials_and_insecure_remote_rejected(self):
        for url in ("https://user:password@host/v1", "http://remote/v1", "https://host/?api_key=x", "https://host/#secret"):
            with self.assertRaises(ValueError):
                Config(url, "x")
        self.assertNotIn("secret-token", repr(Config("https://example.invalid", "secret-token")))


class AppServerContracts(unittest.TestCase):
    def test_stable_request_contracts(self):
        validate("v1/InitializeParams", {"clientInfo": {"name": "medic_p0", "version": "0.1.0"}})
        validate("v2/ThreadStartParams", {"sandbox": "read-only", "approvalPolicy": "never"})
        validate("v2/ThreadResumeParams", {"threadId": "synthetic-thread"})
        validate("v2/TurnStartParams", {"threadId": "synthetic-thread", "input": [{"type": "text", "text": "synthetic"}]})
        validate("v2/TurnInterruptParams", {"threadId": "synthetic-thread", "turnId": "synthetic-turn"})

    def test_invalid_ids_and_missing_input_rejected(self):
        for schema, params in (("v2/TurnStartParams", {"threadId": "x"}),
                               ("v2/TurnInterruptParams", {"threadId": "x", "turnId": 5}),
                               ("v2/ThreadResumeParams", {})):
            with self.assertRaises(jsonschema.ValidationError):
                validate(schema, params)

    def test_mcp_unknown_tool_fails_closed(self):
        logs = []
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "shell", "arguments": {}}}, logs.append)
        self.assertTrue(response["result"]["isError"])
        self.assertFalse(logs[0]["ok"])
        self.assertEqual(handle({"method": "notifications/initialized"}, logs.append), None)


class AcceptanceGate(unittest.TestCase):
    def reports(self):
        return {"baseline": {"status": "pass", "codex_revision": REVISION, "source_matches_revision": True,
                             "generated_schema_matches": True, "build": {"exit_code": 0}, "binary": {"sha256": "binary"}},
                "assets": {"status": "pass", "checks": {"model_17": True, "matrix_49_833": True, "policy_partition": True}},
                "provider": {"synthetic_only": True, "endpoint_sha256": "endpoint", "model": "deepseek-v4-flash", "checks": [{"name": x, "status": "pass"} for x in (
                    "responses.tool_roundtrip", "responses.sse", "responses.json_schema", "responses.two_tools",
                    "responses.tool_error_recovery", "responses.bounded_context", "responses.namespace_tool")]},
                "appserver": {"status": "pass", "synthetic_only": True, "binary_sha256": "binary",
                              "endpoint_sha256": "endpoint", "model": "deepseek-v4-flash",
                              "checks": [{"name": x, "status": "pass"} for x in (
                                  "initialize", "mcp_roundtrip", "output_schema", "resume_after_process_restart",
                                  "concurrent_threads", "interrupt")]}}

    def test_feasibility_does_not_publish_business_results(self):
        result = evaluate(self.reports())
        self.assertTrue(result["p0_accepted"])
        self.assertFalse(result["business_publication_allowed"])
        self.assertFalse(result["production_deployment_allowed"])

    def test_missing_live_evidence_blocks(self):
        for key in ("provider", "appserver"):
            reports = self.reports()
            reports[key]["checks"] = []
            self.assertFalse(evaluate(reports)["p0_accepted"])

    def test_wrong_binary_or_modified_source_blocks(self):
        reports = self.reports()
        reports["appserver"]["binary_sha256"] = "different"
        self.assertFalse(evaluate(reports)["p0_accepted"])
        reports = self.reports()
        reports["baseline"]["source_matches_revision"] = False
        self.assertFalse(evaluate(reports)["p0_accepted"])

    def test_different_customer_endpoint_cannot_supply_evidence(self):
        reports = self.reports()
        reports["appserver"]["endpoint_sha256"] = "different-endpoint"
        self.assertFalse(evaluate(reports)["p0_accepted"])

    def test_gateway_requires_transform_and_cancellation_evidence(self):
        reports = self.reports()
        reports["provider"]["checks"] = [x for x in reports["provider"]["checks"] if x["name"] != "responses.namespace_tool"]
        reports["appserver"]["route"] = "responses_namespace_gateway"
        self.assertFalse(evaluate(reports)["p0_accepted"])
        reports["appserver"]["gateway_trace"] = [{"namespaced_tools": 1, "terminal": "response.completed"}]
        self.assertFalse(evaluate(reports)["p0_accepted"])
        reports["appserver"]["gateway_trace"].append({"client_disconnected": True, "finished": True, "terminal": "not_observed"})
        self.assertTrue(evaluate(reports)["p0_accepted"])


if __name__ == "__main__":
    unittest.main()
