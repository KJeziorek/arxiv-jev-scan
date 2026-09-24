"""JevClient against a mocked HTTP API (no network, no API key needed)."""

import json

import httpx2  # the HTTP library used by typesafe-sdk
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeAuthenticationError, TypeSafeClient

from scanner.jev_client import FATAL_ERRORS, JevClient
from tests.conftest import DOMAIN, RELEVANCE

STATE = {"title": "Event-based optical flow", "abstract": "We estimate flow from a DVS."}


def jev_with(handler, price=0.042) -> JevClient:
    client = TypeSafeClient(
        api_key="test-key",
        transport=httpx2.MockTransport(handler),
        retry=RetryPolicy(max_retries=3, backoff_initial=0.01, backoff_max=0.02),
    )
    return JevClient(client=client, price_per_million=price)


def response(answers: dict, input_tokens=300, output_tokens=20) -> httpx2.Response:
    return httpx2.Response(200, json={
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def test_noul_request_and_answer():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return response({"relevance": {"type": "noul", "noul": 0.93}})

    answer = jev_with(handler).ask(RELEVANCE, STATE)

    assert sent[0]["state"] == STATE
    assert sent[0]["questions"] == {
        "relevance": {"type": "noul", "instructions": RELEVANCE.instructions}
    }
    assert answer.result == {"probability": 0.93}
    assert answer.model == "jev-1.13.0"


def test_choice_answer_keeps_all_probabilities():
    def handler(request):
        question = json.loads(request.content)["questions"]["domain"]
        assert question["criteria"] == DOMAIN.criteria
        return response({"domain": {
            "type": "choice",
            "choice": "Robotics",
            "probabilities": {"Robotics": 0.7, "Vision": 0.3, "Other": 0.0},
            "confidence": 0.55,
        }})

    answer = jev_with(handler).ask(DOMAIN, STATE)

    assert answer.result == {
        "selected": "Robotics",
        "probabilities": {"Robotics": 0.7, "Vision": 0.3, "Other": 0.0},
        "confidence": 0.55,
    }


def test_cost_counts_input_tokens_only():
    def handler(request):
        return response({"relevance": {"type": "noul", "noul": 0.5}}, input_tokens=2_000_000, output_tokens=500)

    answer = jev_with(handler, price=0.042).ask(RELEVANCE, STATE)

    assert answer.input_tokens == 2_000_000
    assert answer.output_tokens == 500
    assert answer.cost_usd == pytest.approx(0.084)


def test_rate_limit_is_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx2.Response(429, json={"detail": "Too Many Requests"})
        return response({"relevance": {"type": "noul", "noul": 0.9}})

    answer = jev_with(handler).ask(RELEVANCE, STATE)

    assert len(attempts) == 3
    assert answer.result["probability"] == 0.9


def test_bad_api_key_is_a_fatal_error():
    def handler(request):
        return httpx2.Response(401, json={"detail": "Invalid API key"})

    with pytest.raises(TypeSafeAuthenticationError):
        jev_with(handler).ask(RELEVANCE, STATE)
    assert TypeSafeAuthenticationError in FATAL_ERRORS


def test_missing_api_key_gives_a_clear_message(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        JevClient()
