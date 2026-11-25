"""``/recall status`` (REQ-029 plugin half): the service's status as one line per index."""

from __future__ import annotations

from .client import Client, Unavailable


def render(client: Client) -> str:
    try:
        status = client.status()
    except Unavailable as exc:
        return "recall_unavailable: %s" % " ".join(exc.reason.split())
    rebuild = status.get("rebuild") if isinstance(status.get("rebuild"), dict) else {}
    state = str(rebuild.get("state"))
    if rebuild.get("progress") is not None:
        state += " (%s)" % rebuild["progress"]
    model = status.get("model")
    indexes = [i for i in status.get("indexes") or [] if isinstance(i, dict)]
    rebuilding = rebuild.get("index")  # the one index the rebuild works on; the others are idle
    lines = ["%s: %s chunks, %s sources, last indexed %s, last error %s, model %s, rebuild %s" % (
        i.get("profile"), i.get("chunks"), i.get("sources"), i.get("last_indexed") or "never indexed",
        i.get("last_error") or "none", model, state if rebuilding in (None, i.get("profile")) else "idle")
        for i in indexes]
    if indexes and rebuilding not in (None, *(i.get("profile") for i in indexes)):
        lines.append("rebuild %s" % state.replace(" (", " (%s, " % rebuilding, 1) if "(" in state
                     else "rebuild %s (%s)" % (state, rebuilding))
    lines = lines or ["no indexes yet: model %s, rebuild %s" % (model, state)]
    if status.get("last_error"):
        lines.append("service last error: %s" % " ".join(str(status["last_error"]).split()))
    metrics = status.get("eval") if isinstance(status.get("eval"), dict) else {}
    lines += ["eval %s: %s" % (name, ", ".join("%s %s" % kv for kv in values.items()))
              for name, values in sorted(metrics.items()) if isinstance(values, dict)]
    return "\n".join(lines)
