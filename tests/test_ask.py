"""Acceptance tests for T-07: async fan-out to Jev with retry, timeout, record and
replay.

One test per acceptance clause in `tasks/typesafe-reviewer/T-07-ask-fanout.md`. No
test opens a network socket: every test that touches a client passes a fake
`httpx2.AsyncBaseTransport` via `transport=`, and `test_guard_no_real_transport`
proves that seam is the only one exercised.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx2
import msgspec
import pytest
from typesafe_sdk import Noul, RetryPolicy, Score, TypeSafeAPIConnectionError, TypeSafeRateLimitError

from typesafe_review.ask import AskFailed, AskResult, Live, Record, Replay, ReplayMiss, ask_all, request_key

FIXTURES = Path(__file__).parent / 'fixtures' / 'responses'

QUESTIONS_A = {
    'swallows_exception': Noul(instructions='q1?'),
    'mixed_responsibility': Score(instructions='q2?', criteria=['a', 'b', 'c']),
}
QUESTIONS_B = {'bare_except': Noul(instructions='q3?')}
MODEL = 'jev-latest'


@pytest.fixture(autouse=True)
def _dummy_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder key so `AsyncTypeSafeClient` construction succeeds against a fake
    transport. Never the real secret: nothing here ever reaches a real socket."""
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-key-not-a-secret')


def _seed_replay_dir(tmp_path: Path, entries: list[tuple[str, dict, dict, str]]) -> Path:
    """Copy fixture bodies into a replay dir, named by the real `request_key` hash."""
    replay_dir = tmp_path / 'responses'
    replay_dir.mkdir()
    for fixture_name, state, questions, model in entries:
        key = request_key(state, questions, model)
        shutil.copy(FIXTURES / fixture_name, replay_dir / f'{key}.json')
    return replay_dir


class _JSONTransport(httpx2.AsyncBaseTransport):
    """A fake transport that always returns one fixed JSON body and status."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        return httpx2.Response(self.status, content=self.body, request=request)


class _ConcurrencyTransport(httpx2.AsyncBaseTransport):
    """Tracks the maximum number of requests in flight at once."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.current = 0
        self.max_seen = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1
        return httpx2.Response(200, content=self.body, request=request)


def _body(model: str, input_tokens: int, answers: dict) -> bytes:
    return json.dumps({'model': model, 'usage': {'input_tokens': input_tokens}, 'answers': answers}).encode()


@pytest.mark.asyncio
async def test_replay_returns_answers_keyed_by_question_id(tmp_path: Path) -> None:
    replay_dir = _seed_replay_dir(
        tmp_path,
        [
            ('noul_and_score.json', {'h': 'one'}, QUESTIONS_A, MODEL),
            ('single_noul.json', {'h': 'two'}, QUESTIONS_B, MODEL),
        ],
    )
    result = await ask_all(
        [('hunk-1', {'h': 'one'}, QUESTIONS_A), ('hunk-2', {'h': 'two'}, QUESTIONS_B)],
        model=MODEL,
        recorder=Replay(replay_dir),
    )
    assert isinstance(result, AskResult)
    assert result.answers['hunk-1'].nouls['swallows_exception'].noul == pytest.approx(0.82)
    assert result.answers['hunk-1'].scores['mixed_responsibility'].score == pytest.approx(1.4)
    assert result.answers['hunk-2'].nouls['bare_except'].noul == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_replay_miss_raises_ask_failed(tmp_path: Path) -> None:
    replay_dir = tmp_path / 'responses'
    replay_dir.mkdir()
    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'missing'}, QUESTIONS_A)],
            model=MODEL,
            recorder=Replay(replay_dir),
        )
    assert isinstance(excinfo.value.__cause__, ReplayMiss)
    assert excinfo.value.__cause__.hunk_key == 'hunk-1'
    assert excinfo.value.key == request_key({'h': 'missing'}, QUESTIONS_A, MODEL)


@pytest.mark.asyncio
async def test_replay_entry_that_is_a_directory_raises_ask_failed(tmp_path: Path) -> None:
    """`Replay.load` only special-cases `FileNotFoundError`; any other `OSError`
    reading the cache (a directory sitting where the file should be, a permissions
    error, ...) must still surface as `AskFailed`, not escape unwrapped."""
    replay_dir = tmp_path / 'responses'
    replay_dir.mkdir()
    key = request_key({'h': 'x'}, QUESTIONS_B, MODEL)
    (replay_dir / f'{key}.json').mkdir()  # a directory, not a file

    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'x'}, QUESTIONS_B)],
            model=MODEL,
            recorder=Replay(replay_dir),
        )
    assert isinstance(excinfo.value.__cause__, IsADirectoryError)
    assert excinfo.value.key == key


@pytest.mark.asyncio
async def test_record_dir_path_that_is_a_file_raises_ask_failed(tmp_path: Path) -> None:
    """`Record.save`'s `mkdir`/`write_bytes` can fail (the target directory path is
    already a file); that must surface as `AskFailed`, not an unwrapped `OSError`."""
    not_a_dir = tmp_path / 'recorded'
    not_a_dir.write_text('this is a file, not a directory')

    body = _body(MODEL, 1, {'bare_except': {'type': 'noul', 'noul': 0.1}})
    transport = _JSONTransport(body=body)
    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'x'}, QUESTIONS_B)],
            model=MODEL,
            recorder=Record(not_a_dir),
            transport=transport,
        )
    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_corrupt_cached_body_raises_ask_failed(tmp_path: Path) -> None:
    """A cached response file that is not valid JSON must surface as `AskFailed`,
    not an unwrapped decode error."""
    replay_dir = tmp_path / 'responses'
    replay_dir.mkdir()
    key = request_key({'h': 'x'}, QUESTIONS_B, MODEL)
    (replay_dir / f'{key}.json').write_bytes(b'not json')

    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'x'}, QUESTIONS_B)],
            model=MODEL,
            recorder=Replay(replay_dir),
        )
    assert excinfo.value.key == key
    assert excinfo.value.__cause__ is not None


@pytest.mark.asyncio
async def test_rate_limit_exhausts_retries_then_ask_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)

    transport = _JSONTransport(body=b'{}', status=429)
    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'x'}, QUESTIONS_B)],
            model=MODEL,
            recorder=Live(),
            transport=transport,
        )
    assert isinstance(excinfo.value.__cause__, TypeSafeRateLimitError)
    # RetryPolicy(max_retries=3) -> exactly 4 attempts (initial + 3 retries).
    assert transport.calls == RetryPolicy(max_retries=3).max_retries + 1 == 4


class _ConnectErrorTransport(httpx2.AsyncBaseTransport):
    """A fake transport that fails to connect on every attempt."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        raise httpx2.ConnectError('connection refused', request=request)


@pytest.mark.asyncio
async def test_connection_error_exhausts_retries_then_ask_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TypeSafeAPIConnectionError` is a sibling of `TypeSafeAPIError` under
    `TypeSafeError`, not a subclass of it -- a bare `except TypeSafeAPIError` would
    let this one escape `ask_all` unwrapped after retries."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)

    transport = _ConnectErrorTransport()
    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('hunk-1', {'h': 'x'}, QUESTIONS_B)],
            model=MODEL,
            recorder=Live(),
            transport=transport,
        )
    assert isinstance(excinfo.value.__cause__, TypeSafeAPIConnectionError)
    # The SDK's RetryPolicy retries connection errors by default (`api_connection_error=True`),
    # so this is also exactly `max_retries + 1` attempts.
    assert transport.calls == RetryPolicy(max_retries=3).max_retries + 1 == 4


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_semaphore() -> None:
    body = _body(MODEL, 10, {'bare_except': {'type': 'noul', 'noul': 0.1}})
    transport = _ConcurrencyTransport(body)
    states = [(f'hunk-{i}', {'h': i}, QUESTIONS_B) for i in range(6)]
    result = await ask_all(states, model=MODEL, concurrency=2, recorder=Live(), transport=transport)
    assert transport.max_seen <= 2
    assert len(result.answers) == 6


@pytest.mark.asyncio
async def test_input_tokens_sum_across_requests(tmp_path: Path) -> None:
    replay_dir = _seed_replay_dir(
        tmp_path,
        [
            ('noul_and_score.json', {'h': 'one'}, QUESTIONS_A, MODEL),
            ('single_noul.json', {'h': 'two'}, QUESTIONS_B, MODEL),
        ],
    )
    result = await ask_all(
        [('hunk-1', {'h': 'one'}, QUESTIONS_A), ('hunk-2', {'h': 'two'}, QUESTIONS_B)],
        model=MODEL,
        recorder=Replay(replay_dir),
    )
    # noul_and_score.json has input_tokens=120, single_noul.json has input_tokens=340.
    assert result.input_tokens == 120 + 340
    assert result.requests == 2
    assert result.model == MODEL


@pytest.mark.asyncio
async def test_result_model_comes_from_the_response_not_the_argument(tmp_path: Path) -> None:
    """`model_mismatch_from_request.json` answers with `"model": "jev-2026-09"` even
    though the fan-out is requested (and the fixture is keyed) with `model=MODEL`
    ("jev-latest") -- a stand-in for a server-side alias resolving to a concrete
    model. `AskResult.model` must read the response's `model`, not echo the argument:
    a mutant that hardcodes `model=model` on the return value would pass every other
    test in this file, because every other fixture happens to use the same string."""
    replay_dir = _seed_replay_dir(
        tmp_path,
        [('model_mismatch_from_request.json', {'h': 'one'}, QUESTIONS_B, MODEL)],
    )
    result = await ask_all(
        [('hunk-1', {'h': 'one'}, QUESTIONS_B)],
        model=MODEL,
        recorder=Replay(replay_dir),
    )
    assert result.model == 'jev-2026-09'
    assert result.model != MODEL


@pytest.mark.asyncio
async def test_record_writes_one_file_per_request(tmp_path: Path) -> None:
    record_dir = tmp_path / 'recorded'
    body = _body(MODEL, 42, {'bare_except': {'type': 'noul', 'noul': 0.3}})
    transport = _JSONTransport(body=body)
    states = [(f'hunk-{i}', {'h': i}, QUESTIONS_B) for i in range(3)]
    await ask_all(states, model=MODEL, recorder=Record(record_dir), transport=transport)
    written = sorted(record_dir.glob('*.json'))
    assert len(written) == 3
    for path in written:
        assert msgspec.json.decode(path.read_bytes())['model'] == MODEL


@pytest.mark.asyncio
async def test_model_disagreement_raises_ask_failed() -> None:
    class _MismatchTransport(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.n = 0

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            self.n += 1
            model = MODEL if self.n == 1 else 'some-other-model'
            body = _body(model, 1, {'bare_except': {'type': 'noul', 'noul': 0.1}})
            return httpx2.Response(200, content=body, request=request)

    with pytest.raises(AskFailed):
        await ask_all(
            [('hunk-1', {'h': 1}, QUESTIONS_B), ('hunk-2', {'h': 2}, QUESTIONS_B)],
            model=MODEL,
            recorder=Live(),
            transport=_MismatchTransport(),
        )


class _SlowTransport(httpx2.AsyncBaseTransport):
    """Sleeps far longer than a sibling failure takes, then would answer -- unless
    cancelled first. Records whether it was actively cancelled mid-sleep, so the
    test can tell "cancelled" apart from "the assertion just ran early"."""

    def __init__(self, body: bytes, delay: float = 0.2) -> None:
        self.body = body
        self.delay = delay
        self.completed = False
        self.cancelled = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return httpx2.Response(200, content=self.body, request=request)


class _FailFastOrLiveRecorder:
    """`fail` hunks miss the (empty) replay cache instantly; anything else goes live."""

    def load(self, key: str, hunk_key: str) -> bytes | None:
        if hunk_key == 'fail':
            raise ReplayMiss(key, hunk_key)
        return None

    def save(self, key: str, body: bytes) -> None:
        return None


@pytest.mark.asyncio
async def test_one_failure_cancels_slow_siblings() -> None:
    body = _body(MODEL, 1, {'swallows_exception': {'type': 'noul', 'noul': 0.1}})
    transport = _SlowTransport(body)
    with pytest.raises(AskFailed) as excinfo:
        await ask_all(
            [('fail', {'h': 1}, QUESTIONS_B), ('slow', {'h': 2}, QUESTIONS_A)],
            model=MODEL,
            recorder=_FailFastOrLiveRecorder(),
            transport=transport,
        )
    assert isinstance(excinfo.value.__cause__, ReplayMiss)
    # The slow sibling was actively cancelled mid-flight, not merely still pending:
    # `ask_all` awaits the cancellation before returning, so this is deterministic.
    assert transport.cancelled is True
    assert transport.completed is False


def test_guard_no_real_transport_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the real socket transport must fail loudly if anything tries it."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError('a test tried to construct a real network transport')

    monkeypatch.setattr(httpx2, 'AsyncHTTPTransport', _boom)

    async def run() -> AskResult:
        body = _body(MODEL, 1, {'bare_except': {'type': 'noul', 'noul': 0.1}})
        transport = _JSONTransport(body=body)
        return await ask_all([('hunk-1', {'h': 1}, QUESTIONS_B)], model=MODEL, recorder=Live(), transport=transport)

    result = asyncio.run(run())
    assert result.answers['hunk-1'].nouls['bare_except'].noul == pytest.approx(0.1)


def test_request_key_is_deterministic() -> None:
    a = request_key({'x': 1}, QUESTIONS_A, MODEL)
    b = request_key({'x': 1}, QUESTIONS_A, MODEL)
    c = request_key({'x': 2}, QUESTIONS_A, MODEL)
    assert a == b
    assert a != c
    assert isinstance(a, str) and len(a) == 64

    # Same key regardless of dict insertion order.
    reordered = {
        'mixed_responsibility': QUESTIONS_A['mixed_responsibility'],
        'swallows_exception': QUESTIONS_A['swallows_exception'],
    }
    assert request_key({'x': 1}, reordered, MODEL) == a

    # Same key for two separately constructed but equal Noul objects.
    same_questions = {'bare_except': Noul(instructions='q3?')}
    assert request_key({'x': 1}, same_questions, MODEL) == request_key({'x': 1}, QUESTIONS_B, MODEL)

    # Different key for a changed model.
    assert request_key({'x': 1}, QUESTIONS_A, 'a-different-model') != a


@pytest.mark.asyncio
async def test_client_is_closed_after_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from typesafe_sdk import AsyncTypeSafeClient

    closed: list[bool] = []
    original_aclose = AsyncTypeSafeClient.aclose

    async def tracking_aclose(self: AsyncTypeSafeClient) -> None:
        closed.append(True)
        await original_aclose(self)

    monkeypatch.setattr(AsyncTypeSafeClient, 'aclose', tracking_aclose)

    body = _body(MODEL, 1, {'bare_except': {'type': 'noul', 'noul': 0.1}})
    transport = _JSONTransport(body=body)
    await ask_all([('hunk-1', {'h': 1}, QUESTIONS_B)], model=MODEL, recorder=Live(), transport=transport)
    assert closed == [True]


@pytest.mark.asyncio
async def test_client_is_closed_even_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from typesafe_sdk import AsyncTypeSafeClient

    closed: list[bool] = []
    original_aclose = AsyncTypeSafeClient.aclose

    async def tracking_aclose(self: AsyncTypeSafeClient) -> None:
        closed.append(True)
        await original_aclose(self)

    monkeypatch.setattr(AsyncTypeSafeClient, 'aclose', tracking_aclose)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)

    transport = _JSONTransport(body=b'{}', status=429)
    with pytest.raises(AskFailed):
        await ask_all([('hunk-1', {'h': 'x'}, QUESTIONS_B)], model=MODEL, recorder=Live(), transport=transport)
    assert closed == [True]


@pytest.mark.asyncio
async def test_empty_states_returns_empty_result() -> None:
    result = await ask_all([], model=MODEL, recorder=Live())
    assert result.answers == {}
    assert result.requests == 0
    assert result.input_tokens == 0
