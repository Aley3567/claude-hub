import asyncio
import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "claude1_transport.py"


def load_transport_module():
    spec = importlib.util.spec_from_file_location("claude1_transport", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status=200):
        self.status = status
        self.headers = {}


class _RequestContext:
    def __init__(self, result):
        self.result = result

    async def __aenter__(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _RequestContext(self.results.pop(0))


class TransportIntegrationTests(unittest.TestCase):
    def test_auto_retries_pre_response_direct_failure_through_system_proxy(self):
        transport = load_transport_module()
        policy = transport.resolve_transport_policy(
            "https://api.example.test/v1/messages",
            {"mode": "auto", "proxies": ["system"]},
            environ={"HTTPS_PROXY": "http://127.0.0.1:7897"},
            system_proxies={"https": "http://127.0.0.1:7897"},
            bypass=lambda _host: False,
        )
        session = _Session([OSError("dns unavailable"), _Response(200)])

        async def exercise():
            executor = transport.UpstreamExecutor()
            async with executor.open(
                session=session,
                method="POST",
                url="https://api.example.test/v1/messages",
                policy=policy,
                request_kwargs={"data": b"{}"},
            ) as attempt:
                self.assertEqual(attempt.response.status, 200)
                self.assertEqual(attempt.identity, "proxy:http://127.0.0.1:7897")

        asyncio.run(exercise())
        self.assertEqual(
            [call[2].get("proxy") for call in session.calls],
            [None, "http://127.0.0.1:7897"],
        )

    def test_diagnose_reports_direct_failure_and_working_proxy(self):
        transport = load_transport_module()
        policy = transport.resolve_transport_policy(
            "https://api.example.test/v1/messages",
            {"mode": "auto", "proxies": ["http://127.0.0.1:7897"]},
            bypass=lambda _host: False,
        )

        report = transport.diagnose_transport_policy(
            "https://api.example.test/v1/messages",
            policy,
            probe=lambda candidate, _endpoint, _timeout: (
                (False, "dns: timed out")
                if candidate.proxy is None
                else (True, "HTTP 405")
            ),
        )

        self.assertFalse(report[0].ok)
        self.assertEqual(report[0].identity, "direct")
        self.assertTrue(report[1].ok)
        self.assertEqual(report[1].identity, "proxy:http://127.0.0.1:7897")

    def test_retryable_403_tries_proxy_before_returning_response(self):
        transport = load_transport_module()
        policy = transport.resolve_transport_policy(
            "https://api.example.test/v1/messages",
            {"mode": "auto", "proxies": ["http://127.0.0.1:7897"]},
            bypass=lambda _host: False,
        )
        session = _Session([_Response(403), _Response(200)])

        async def exercise():
            async with transport.UpstreamExecutor().open(
                session=session,
                method="POST",
                url="https://api.example.test/v1/messages",
                policy=policy,
                request_kwargs={"data": b"{}"},
                retry_response=lambda response: response.status == 403,
            ) as attempt:
                self.assertEqual(attempt.response.status, 200)
                self.assertEqual(
                    attempt.identity,
                    "proxy:http://127.0.0.1:7897",
                )

        asyncio.run(exercise())
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
