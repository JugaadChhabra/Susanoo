"""Unit tests for integrations/llm.py. A fake client stands in for the SDK: no API calls."""

import asyncio
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from susanoo.integrations import llm


def _response(text="ok", input_tokens=100, output_tokens=50, stop_reason="end_turn", extra_blocks=()):
    blocks = [SimpleNamespace(type="text", text=text), *extra_blocks]
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


class FakeClient:
    """Plays back `script` in order: exceptions are raised, anything else is returned."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **request):
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm.random, "uniform", lambda a, b: 0.0)
    return recorded


def run_call(client, head="planner", **kw):
    messages = kw.pop("messages", [{"role": "user", "content": "hi"}])
    return asyncio.run(llm.call(head, messages, client=client, **kw))


# --- model routing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "model"),
    [
        ("planner", "claude-opus-5-5"),
        ("diagnostician", "claude-opus-5-5"),
        ("builder", "claude-sonnet-5-5"),
        ("agent_smith", "claude-sonnet-5-5"),
        ("fixer", "claude-sonnet-5-5"),
        ("simulator", "claude-haiku-5-5"),
        ("translate", "claude-haiku-5-5"),
    ],
)
def test_head_picks_its_model(head, model):
    client = FakeClient([_response()])
    result = run_call(client, head=head)
    assert client.requests[0]["model"] == model
    assert result.model == model


def test_unknown_head_raises_before_any_request():
    client = FakeClient([_response()])
    with pytest.raises(ValueError, match="unknown head 'runner'"):
        run_call(client, head="runner")
    assert client.requests == []


def test_request_carries_messages_system_max_tokens_and_extra_kwargs():
    client = FakeClient([_response()])
    msgs = [{"role": "user", "content": "plan this"}]
    run_call(client, messages=msgs, system="be brief", max_tokens=123, output_config={"effort": "low"})
    req = client.requests[0]
    assert req["messages"] == msgs
    assert req["system"] == "be brief"
    assert req["max_tokens"] == 123
    assert req["output_config"] == {"effort": "low"}


def test_no_system_key_when_system_not_given():
    client = FakeClient([_response()])
    run_call(client)
    assert "system" not in client.requests[0]


# --- result, tokens and cost -------------------------------------------------


def test_result_has_text_tokens_and_cost():
    client = FakeClient([_response(text="hello", input_tokens=1_000_000, output_tokens=500_000)])
    result = run_call(client, head="planner")  # opus: $4 in, $20 out per 1M
    assert result.text == "hello"
    assert result.input_tokens == 1_000_000
    assert result.output_tokens == 500_000
    assert result.cost_usd == pytest.approx(4.00 + 10.00)


def test_cost_uses_the_pricing_table(monkeypatch):
    monkeypatch.setitem(llm.PRICING_USD_PER_MTOK, llm.SONNET, {"input": 10.0, "output": 100.0})
    client = FakeClient([_response(input_tokens=200_000, output_tokens=10_000)])
    result = run_call(client, head="builder")
    assert result.cost_usd == pytest.approx(2.0 + 1.0)


def test_every_head_model_has_a_price():
    assert set(llm.HEAD_MODELS.values()) <= set(llm.PRICING_USD_PER_MTOK)


def test_text_blocks_are_joined_and_other_blocks_ignored():
    thinking = SimpleNamespace(type="thinking", thinking="...")
    client = FakeClient([_response(text="answer", extra_blocks=[thinking, SimpleNamespace(type="text", text=" more")])])
    assert run_call(client).text == "answer more"


def test_refusal_raises():
    client = FakeClient([_response(stop_reason="refusal")])
    with pytest.raises(llm.LLMRefusal):
        run_call(client)


# --- JSON ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', '```\n{"a": 1}\n```', '  {"a": 1}\n'],
)
def test_json_parses_plain_and_fenced(text):
    client = FakeClient([_response(text=text)])
    assert run_call(client).json() == {"a": 1}


def test_json_raises_on_non_json():
    client = FakeClient([_response(text="sorry, no")])
    result = run_call(client)
    with pytest.raises(ValueError):
        result.json()


# --- retries ------------------------------------------------------------------


def test_retries_transient_errors_with_exponential_backoff(sleeps):
    client = FakeClient(
        [
            _status_error(anthropic.RateLimitError, 429),
            _status_error(anthropic.InternalServerError, 500),
            _status_error(anthropic.APIStatusError, 529),
            _response(text="finally"),
        ]
    )
    result = run_call(client)
    assert result.text == "finally"
    assert len(client.requests) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retries_connection_errors(sleeps):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient([anthropic.APIConnectionError(request=request), _response()])
    assert run_call(client).text == "ok"
    assert len(sleeps) == 1


def test_backoff_is_capped(sleeps):
    n = llm.RETRY_MAX_ATTEMPTS
    client = FakeClient([_status_error(anthropic.RateLimitError, 429)] * (n - 1) + [_response()])
    run_call(client)
    assert max(sleeps) <= llm.RETRY_MAX_DELAY_S


def test_gives_up_after_max_attempts_and_raises_the_last_error(sleeps):
    client = FakeClient([_status_error(anthropic.RateLimitError, 429)] * llm.RETRY_MAX_ATTEMPTS)
    with pytest.raises(anthropic.RateLimitError):
        run_call(client)
    assert len(client.requests) == llm.RETRY_MAX_ATTEMPTS
    assert len(sleeps) == llm.RETRY_MAX_ATTEMPTS - 1


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (anthropic.BadRequestError, 400),
        (anthropic.AuthenticationError, 401),
        (anthropic.PermissionDeniedError, 403),
        (anthropic.NotFoundError, 404),
    ],
)
def test_client_errors_are_not_retried(sleeps, cls, status):
    client = FakeClient([_status_error(cls, status), _response()])
    with pytest.raises(cls):
        run_call(client)
    assert len(client.requests) == 1
    assert sleeps == []


def test_non_api_errors_are_not_retried(sleeps):
    client = FakeClient([RuntimeError("bug"), _response()])
    with pytest.raises(RuntimeError):
        run_call(client)
    assert len(client.requests) == 1
