"""Async fan-out to Jev: one `system_one` request per state, with retry, timeout,
record and replay (T-07).

Spec §4 steps 4-5, §4.0, §6.7. `ask_all` sends every applicable question for one state
in one request, bounded by a semaphore, and returns raw answers plus usage; nothing
here decides severity, threshold, or verdict -- that is `compose.py` (T-08).

Every request is looked up in a `Recorder` first (`Replay` reads a cached body from
disk and never touches the network; `Record` and `Live` always call the network, and
`Record` also saves the raw response body). A request that does not get an answer --
an API error surviving retries, a replay miss, or model disagreement across
responses -- never becomes an entry in `AskResult.answers`; it raises `AskFailed`
instead, wrapping the original exception as `__cause__`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx2
import msgspec
from typesafe_sdk import AsyncTypeSafeClient, JSONContent, Question, RetryPolicy, SystemOneResponse, TypeSafeError

#: One fan-out item: the caller's own key for the state (a hunk id, or "change"), the
#: state itself, and the questions to ask about it.
RequestItem = tuple[str, JSONContent, Mapping[str, Question]]

_TIMEOUT_SECONDS = 60
_MAX_RETRIES = 3


def request_key(state: JSONContent, questions: Mapping[str, Question], model: str) -> str:
    """The record/replay key for one request: `sha256(canonical_json({state, questions, model}))`.

    Canonical means the same key for the same inputs regardless of dict insertion
    order: everything is converted to plain JSON values, then dumped with sorted keys.
    """
    payload = {'state': state, 'questions': dict(questions), 'model': model}
    canonical = json.dumps(msgspec.to_builtins(payload), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def response_from_bytes(key: str, raw: bytes) -> SystemOneResponse:
    """Parse `raw` (a cached or recorded response body) into a `SystemOneResponse`,
    wrapping any parse failure into `AskFailed` -- the same failure contract a live
    response gets in `ask_all`'s own `one()`. Public: `calibrate.py`'s state-kind
    case path replays straight from `responses/` without going through `ask_all`,
    but a corrupt cache file there must fail exactly as loudly, not with a raw SDK
    exception (RA-04 fix round 1)."""
    try:
        return SystemOneResponse.from_http_response(httpx2.Response(200, content=raw))
    except (TypeSafeError, OSError) as error:
        raise AskFailed(key, error) from error


class ReplayMiss(Exception):
    """No recorded response exists for a request key, naming the hunk it was for."""

    def __init__(self, key: str, hunk_key: str) -> None:
        super().__init__(f'no recorded response for hunk {hunk_key!r} (request key {key})')
        self.key = key
        self.hunk_key = hunk_key


class ReplayMissByKey(ReplayMiss):
    """`ReplayMiss` raised from `Replay.load_by_key`: there is no hunk identity to
    name, just the `request_key` itself (RA-04 fix round 1 MINOR: the base
    class's message unconditionally said "hunk", which read wrong for a
    state-kind case looked up by `request_key` alone). Still an instance of
    `ReplayMiss`, so an existing `except ReplayMiss` catches it too."""

    def __init__(self, key: str) -> None:
        Exception.__init__(self, f'no recorded response for request key {key!r}')
        self.key = key
        self.hunk_key = key


class AskFailed(Exception):
    """A request never got an answer: an API error survived retries, a replay missed,
    or responses disagreed on which model answered them.

    Always carries the request `key` and wraps the original exception as `__cause__`.
    """

    def __init__(self, key: str, cause: BaseException) -> None:
        super().__init__(f'request {key} failed: {cause}')
        self.key = key


class Recorder(Protocol):
    """Where a response for one request key comes from, and where it is saved.

    `load` returns the cached raw response body for `key`, or `None` to fetch live;
    a recorder that never has a cache entry (`Live`, `Record`) always returns `None`.
    `save` persists a raw response body; a recorder that never caches ignores it.
    """

    def load(self, key: str, hunk_key: str) -> bytes | None: ...

    def save(self, key: str, body: bytes) -> None: ...


@dataclass(frozen=True)
class Live:
    """Always calls the network; never reads or writes a cache."""

    def load(self, key: str, hunk_key: str) -> bytes | None:
        return None

    def save(self, key: str, body: bytes) -> None:
        return None


@dataclass(frozen=True)
class Record:
    """Always calls the network, and saves every response to `dir` as `<key>.json`."""

    dir: Path

    def load(self, key: str, hunk_key: str) -> bytes | None:
        return None

    def save(self, key: str, body: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f'{key}.json').write_bytes(body)


@dataclass(frozen=True)
class Replay:
    """Never touches the network: reads a cached response from `dir`, or raises `ReplayMiss`."""

    dir: Path

    def load(self, key: str, hunk_key: str) -> bytes:
        path = self.dir / f'{key}.json'
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ReplayMiss(key, hunk_key) from error

    def save(self, key: str, body: bytes) -> None:
        return None

    def load_by_key(self, key: str) -> bytes:
        """Read a cached response body by its `request_key` alone, with no hunk
        identity to name in the error (RA-04): `calibrate.py`'s state-kind case
        path replays straight from a `keys.json`-recorded `request_key`, never
        re-deriving it from a (possibly redacted) dumped state."""
        path = self.dir / f'{key}.json'
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ReplayMissByKey(key) from error


@dataclass(frozen=True)
class AskResult:
    """Every answer from one fan-out, keyed by the caller's own per-state key, plus
    the usage totals the spec's `engine` field needs (§7)."""

    answers: dict[str, SystemOneResponse]
    requests: int
    input_tokens: int
    model: str


async def ask_all(
    states: Sequence[RequestItem],
    *,
    model: str,
    concurrency: int = 4,
    recorder: Recorder,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AskResult:
    """Send one `system_one` request per state, bounded by a semaphore of `concurrency`.

    `transport` is the only seam a caller (or a test) has to fake the network; it is
    passed straight to `AsyncTypeSafeClient`. The client itself is built lazily, on
    the first request the `recorder` cannot answer from a cache, so a fully-replayed
    run never needs a client -- or an API key -- at all.
    """
    if not states:
        return AskResult(answers={}, requests=0, input_tokens=0, model=model)

    semaphore = asyncio.Semaphore(concurrency)
    client: AsyncTypeSafeClient | None = None

    def get_client() -> AsyncTypeSafeClient:
        nonlocal client
        if client is None:
            client = AsyncTypeSafeClient(
                transport=transport,
                model=model,
                retry=RetryPolicy(max_retries=_MAX_RETRIES),
                timeout=_TIMEOUT_SECONDS,
            )
        return client

    async def one(
        hunk_key: str, state: JSONContent, questions: Mapping[str, Question]
    ) -> tuple[str, str, SystemOneResponse]:
        key = request_key(state, questions, model)
        async with semaphore:
            try:
                cached = recorder.load(key, hunk_key)
            except ReplayMiss as error:
                raise AskFailed(key, error) from error
            except OSError as error:
                # A recorder's own I/O failing (a directory where a file was
                # expected, permissions, ...) is exactly as fatal as a miss.
                raise AskFailed(key, error) from error

            started = time.monotonic()
            try:
                if cached is not None:
                    response = response_from_bytes(key, cached)
                else:
                    response = await get_client().system_one(state, questions, model=model)
                    recorder.save(key, response.raw_http_response.content)
            except (TypeSafeError, OSError) as error:
                # `TypeSafeError` catches the whole failure family after retries:
                # `TypeSafeAPIError` (bad status) and `TypeSafeAPIConnectionError` /
                # `TypeSafeAPITimeoutError` (no response at all) are siblings under
                # `TypeSafeError`, not `TypeSafeAPIError` subclasses, and a corrupt
                # cached body surfaces from `from_http_response` as
                # `TypeSafeAPIResponseValidationError`. `OSError` covers `Record.save`
                # writing to a bad path.
                raise AskFailed(key, error) from error

            elapsed_ms = (time.monotonic() - started) * 1000
            _log_request(key, len(questions), response.usage.input_tokens, elapsed_ms)
            return hunk_key, key, response

    tasks = [asyncio.ensure_future(one(hunk_key, state, questions)) for hunk_key, state, questions in states]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        error: BaseException | None = None
        for task in tasks:
            if task in done and not task.cancelled() and task.exception() is not None:
                error = task.exception()
                break

        if error is not None:
            raise error

        results = [task.result() for task in tasks]
    except BaseException:
        # One request failed, or `ask_all` itself was cancelled: a sibling still
        # retrying, sleeping, or writing to disk must not keep running work whose
        # result no one will read.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        if client is not None:
            await client.aclose()

    answers: dict[str, SystemOneResponse] = {}
    input_tokens = 0
    seen_model: str | None = None
    mismatch: tuple[str, str] | None = None
    for hunk_key, key, response in results:
        answers[hunk_key] = response
        input_tokens += response.usage.input_tokens or 0
        if seen_model is None:
            seen_model = response.model
        elif mismatch is None and response.model != seen_model:
            mismatch = (key, response.model)

    if mismatch is not None:
        mismatch_key, mismatch_model = mismatch
        cause = ValueError(f'responses disagree on model: {seen_model!r} vs {mismatch_model!r}')
        raise AskFailed(mismatch_key, cause) from cause

    return AskResult(answers=answers, requests=len(states), input_tokens=input_tokens, model=seen_model or model)


def _log_request(key: str, question_count: int, input_tokens: int | None, elapsed_ms: float) -> None:
    """One stderr line per request: key, question count, input tokens, elapsed ms.

    Never the state or any header/credential -- `key` is a content hash, not key
    material.
    """
    print(
        f'ask key={key} questions={question_count} input_tokens={input_tokens} elapsed_ms={elapsed_ms:.0f}',
        file=sys.stderr,
    )
