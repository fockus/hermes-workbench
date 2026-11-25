"""What an eval attempt's model calls left behind, so a measurement is complete or marked incomplete
(hardening G5d, G5f; D3 Astra round 1).

Stock's order per provider response: ``_last_response_reasoning_tokens`` is written (every response,
before truncation recovery), then a response with usage is counted in ``session_api_calls``, its
tokens are added to the session counters and ``session_cost_status`` is written once. ``watch``
puts the attempt's agent in a subclass whose properties record those writes and every
``session_id`` the agent takes. ``side_calls`` counts the calls the parent's counters never see:
``delegate_task`` (children's spend is rolled up separately), auxiliary responses (compaction,
vision, ...) and stock's iteration-limit summary request. MoA routes are refused before any
attempt (``live.moa_problem``), so every auxiliary response counts. ``unaccounted`` folds both into one count; any value above 0 makes tokens and cost
incomplete.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

_INPUT = (
    "session_input_tokens",
    "session_cache_read_tokens",
    "session_cache_write_tokens",
)


class Receipts:
    def __init__(self, session_id: str) -> None:
        self.prices: list[Any] = []  # every session_cost_status write
        self.empty_prices = (
            0  # price writes with no new input tokens (zero-token usage)
        )
        self.responses = 0
        self.sessions = [session_id]  # every distinct session id, in order
        self.side_calls = 0
        self.watched = False
        self._inputs = 0


def _inputs(agent: Any) -> int:
    return int(sum(agent.__dict__.get(name, 0) or 0 for name in _INPUT))


def watch(agent: Any, session_id: str) -> Receipts:
    """Receipts of ``agent`` from now on; ``watched`` stays False when its class cannot be swapped."""
    receipts = Receipts(session_id)
    receipts._inputs = _inputs(agent)

    def getter(name: str) -> Any:
        def get(self: Any) -> Any:
            try:
                return self.__dict__[name]
            except KeyError:
                raise AttributeError(name) from None

        return get

    def put_price(self: Any, value: Any) -> None:
        self.__dict__["session_cost_status"] = value
        receipts.prices.append(value)
        now = _inputs(self)
        if now <= receipts._inputs:
            receipts.empty_prices += 1
        receipts._inputs = now

    def put_response(self: Any, value: Any) -> None:
        self.__dict__["_last_response_reasoning_tokens"] = value
        receipts.responses += 1

    def put_session(self: Any, value: Any) -> None:
        self.__dict__["session_id"] = value
        if isinstance(value, str) and value and value not in receipts.sessions:
            receipts.sessions.append(value)

    base = type(agent)
    props: dict[str, Any] = {
        "session_cost_status": property(getter("session_cost_status"), put_price),
        "_last_response_reasoning_tokens": property(
            getter("_last_response_reasoning_tokens"), put_response
        ),
        "session_id": property(getter("session_id"), put_session),
    }
    summary = getattr(base, "_handle_max_iterations", None)
    if callable(summary):  # stock's iteration-limit summary is a request outside the counted loop

        def limited(self: Any, *args: Any, **kwargs: Any) -> Any:
            receipts.side_calls += 1
            return summary(self, *args, **kwargs)

        props["_handle_max_iterations"] = limited
    try:
        agent.__class__ = type(base.__name__, (base,), props)
    except TypeError:
        return receipts
    receipts.watched = True
    return receipts


@contextlib.contextmanager
def side_calls(receipts: Receipts) -> Iterator[None]:
    """Count delegations and auxiliary model responses while the attempt runs (the eval runs one
    attempt at a time); a hook that cannot be placed counts as one uncounted call."""
    placed: list[tuple[Any, str, Any]] = []
    try:
        import agent.aux_accounting as aux
        import tools.delegate_tool as delegation
    except ImportError:
        receipts.side_calls += 1
        yield
        return

    def counted(original: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            receipts.side_calls += 1
            return original(*args, **kwargs)

        return call

    for module, name in ((aux, "record_aux_usage"), (delegation, "delegate_task")):
        original = getattr(module, name, None)
        if not callable(original):
            receipts.side_calls += 1
            continue
        setattr(module, name, counted(original))
        placed.append((module, name, original))
    try:
        yield
    finally:
        for module, name, original in placed:
            setattr(module, name, original)


def unaccounted(agent: Any, receipts: Receipts) -> int:
    """Model calls of the attempt its tokens and cost do not cover: requests without a priced,
    non-empty usage, responses stock never counted (truncation continuations), side calls. Every
    request counts when the agent could not be watched."""
    requests = int(getattr(agent, "session_api_calls", 0) or 0)
    if not receipts.watched:
        return max(requests, 1) + receipts.side_calls
    priced = len(receipts.prices) - receipts.empty_prices
    return (
        max(0, requests - priced)
        + abs(receipts.responses - requests)
        + receipts.side_calls
    )
