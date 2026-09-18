import time
import unittest
from unittest.mock import Mock

from app.ai_runner import (
    _is_transient_provider_error,
    _run_with_transient_retries,
    _status_code_from_exception,
)


class ProviderError(RuntimeError):
    def __init__(self, status_code):
        super().__init__("provider failure")
        self.status_code = status_code


class AiRunnerRetryTests(unittest.TestCase):
    def test_status_is_found_on_wrapped_provider_error(self):
        outer = RuntimeError("wrapped")
        outer.__cause__ = ProviderError(503)
        self.assertEqual(_status_code_from_exception(outer), 503)
        self.assertTrue(_is_transient_provider_error(outer))
        self.assertTrue(_is_transient_provider_error(ProviderError(429)))
        self.assertFalse(_is_transient_provider_error(ProviderError(400)))

    def test_retries_transient_errors_and_keeps_nontransient_errors_single_shot(self):
        calls = Mock(side_effect=[ProviderError(503), "ok"])
        sleeps = []
        result = _run_with_transient_retries(
            calls, {"llm_max_retries": 2}, time.monotonic() + 10,
            sleep=sleeps.append,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(sleeps, [1.0])

        failed = Mock(side_effect=ProviderError(400))
        with self.assertRaises(ProviderError):
            _run_with_transient_retries(
                failed, {"llm_max_retries": 2}, time.monotonic() + 10,
                sleep=sleeps.append,
            )
        self.assertEqual(failed.call_count, 1)

    def test_retry_budget_is_bounded_and_deadline_is_respected(self):
        calls = Mock(side_effect=ProviderError(503))
        with self.assertRaises(ProviderError):
            _run_with_transient_retries(
                calls, {"llm_max_retries": 99}, time.monotonic() + 10,
                sleep=lambda _delay: None,
            )
        self.assertEqual(calls.call_count, 4)

        expired = Mock(side_effect=ProviderError(503))
        with self.assertRaises(ProviderError):
            _run_with_transient_retries(
                expired, {"llm_max_retries": 2}, time.monotonic() - 1,
                sleep=lambda _delay: None,
            )
        self.assertEqual(expired.call_count, 1)


if __name__ == "__main__":
    unittest.main()
