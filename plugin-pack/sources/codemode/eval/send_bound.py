"""A hard cap on an eval agent's physical model sends (track CE2): stock retries a request on its own
(``agent.api_max_retries``, ``agent.auto_recovery_cycles``, Codex stream reconnects), so the printed bound
is enforced on the agent, not computed from those settings.

Every main-model request gets its client from ``agent._create_request_openai_client`` or
``_create_request_anthropic_client`` (stock sets ``max_retries=0`` on it: the SDK never re-sends), and goes
out through one of ``_SENDS`` on that client. The cap counts each of those calls and refuses the one past
it with a client error, which ends stock's turn instead of being retried.
A call on the DirectSDK endpoint can re-send once inside itself (the plugin's admission.py
``Handler._allows_auth_retry``: a 401 refreshes the login and the same call sends again), so each of its
calls is charged both sends before it starts: the re-send always fits under the bound, never past it.
The same boundary counts the sends that went out (``sent``), failed ones included: a timeout and its retry are
two requests, though only the answered one has usage in the session counters.
ponytail: the cap charges the per-call worst case, and ``sent`` cannot see the DirectSDK's own re-send inside a
call; a transport hook from stock would count each send.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from typing import Any, Callable, Mapping

from .route_choice import ENDPOINTS

_FACTORIES = ("_create_request_openai_client", "_create_request_anthropic_client")
_SENDS = (
    ("chat", "completions", "create"),
    ("responses", "create"),
    ("responses", "stream"),
    ("messages", "create"),
    ("messages", "stream"),
)
_ORIGINAL, _TAKE = "_codemode_eval_send", "_codemode_eval_take"
_SENT = "_codemode_eval_sent"


def sends_per_call(route: Mapping[str, Any] | None) -> int:
    """Physical sends one model call on ``route`` can make: 2 on the DirectSDK endpoint, else 1."""
    base_url = str((route or {}).get("base_url") or "").rstrip("/")
    return 2 if base_url == ENDPOINTS["haiku"] else 1


class SendRefused(RuntimeError):
    """The send past the cap. ``status_code`` 400: stock classifies a client error as final, not retryable."""

    status_code = 400


def _capped(send: Callable[..., Any], take: Callable[[], None]) -> Callable[..., Any]:
    def capped(*args: Any, **kwargs: Any) -> Any:
        take()
        return send(*args, **kwargs)

    capped.__dict__.update({_ORIGINAL: send, _TAKE: take})  # no functools.wraps: a mock's state stays its own
    return capped


def _guard(client: Any, take: Callable[[], None]) -> Any:
    """``client`` with each send method counted by ``take``; a client with none of them is refused."""
    found = False
    for *path, name in _SENDS:
        owner = client
        for part in path:
            owner = getattr(owner, part, None)
        send = getattr(owner, name, None) if owner is not None else None
        if not callable(send):
            continue
        found = True
        marks = getattr(send, "__dict__", {})  # only a capped send carries the marks (a mock invents attributes)
        if marks.get(_TAKE) is not take:  # uncapped, or another agent's cap: cap the original
            setattr(owner, name, _capped(marks.get(_ORIGINAL, send), take))
    if not found:
        raise SendRefused(
            "codemode eval: the sends of a %s client cannot be counted"
            % type(client).__name__
        )
    return client


def cap(agent: Any, calls: int, per_call: int = 1) -> Any:
    """``agent`` with at most ``calls`` x ``per_call`` physical model sends, each call charged ``per_call``
    up front; the call that would not fit raises ``SendRefused``."""
    lock, sent, limit = threading.Lock(), [0, 0], calls * per_call  # [charged, went out]

    def take() -> None:
        with lock:
            if sent[0] + per_call > limit:
                raise SendRefused(
                    "codemode eval: the send past the bound of %d model requests per attempt was "
                    "refused" % limit
                )
            sent[0] += per_call
            sent[1] += 1

    with contextlib.suppress(AttributeError):  # a slotted agent has no count: ``sent`` reads 0
        setattr(agent, _SENT, sent)

    for name in _FACTORIES:
        factory = getattr(agent, name, None)
        if callable(factory):
            setattr(
                agent,
                name,
                functools.wraps(factory)(
                    lambda *a, _factory=factory, **k: _guard(_factory(*a, **k), take)
                ),
            )
    return agent


def sent(agent: Any) -> int:
    """Model sends that went out on a capped ``agent``, failed ones included (0 for an uncapped agent)."""
    return int((getattr(agent, _SENT, None) or [0, 0])[1])
