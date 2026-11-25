"""``hermes heartbeat install | status``: exactly one stock cron job runs ``hermes heartbeat tick``.

The job is a no-agent script job every 10 minutes; the stock cron delivers the script's stdout (questions,
the digest, a probe warning) to its ``deliver`` targets, which come from the plugin config ``deliver``
(default ``all``: every connected chat and the desktop)."""

from __future__ import annotations

from . import probe

JOB_NAME = "heartbeat"
SCHEDULE = "every 10m"
SCRIPT = "heartbeat-tick.sh"
_SCRIPT_TEXT = "#!/bin/sh\n# written by `hermes heartbeat install`\nexec hermes heartbeat tick\n"


def _deliver(ctx) -> str:
    value = ctx.get_config("deliver")
    if isinstance(value, (list, tuple)):  # stock cron keeps a target list as one comma string
        value = ",".join(str(v).strip() for v in value if str(v).strip()) or "local"  # stock: [] is local
    return value.strip() if isinstance(value, str) and value.strip() else "all"


def _find_job() -> dict | None:
    from cron.jobs import list_jobs

    return next((job for job in list_jobs(include_disabled=True) if job.get("name") == JOB_NAME), None)


def install(ctx) -> str:
    """Create the job, or bring our existing one back to the stock shape; returns the line to print.
    Nothing is written under a named profile (one job, in the main profile: D-02), while the probe
    disables ``install``, or when the job name or the script file belongs to someone else."""
    from hermes_constants import get_hermes_home, named_profile_home

    if named_profile_home(get_hermes_home()) is not None:
        return "heartbeat: not installed - run `hermes heartbeat install` in the main profile (one job watches every board)"
    result = probe.check()
    if "install" in result.disabled:
        return result.warning
    from cron.jobs import parse_schedule, update_job
    from cron.scheduler import create_job_with_scheduler_registration

    script = get_hermes_home() / "scripts" / SCRIPT
    job = _find_job()
    if job is not None and job.get("script") != SCRIPT:
        return "heartbeat: conflict - cron job %s is named %r but runs something else; nothing changed" % (
            job["id"], JOB_NAME)
    if script.exists() and script.read_text(encoding="utf-8", errors="replace") != _SCRIPT_TEXT:
        return "heartbeat: conflict - %s exists with other content; nothing changed" % script
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_SCRIPT_TEXT, encoding="utf-8")
    script.chmod(0o755)
    fields = {"script": SCRIPT, "no_agent": True, "deliver": _deliver(ctx)}
    if job is None:
        job = create_job_with_scheduler_registration(prompt=None, schedule=SCHEDULE, name=JOB_NAME, **fields)
        return "heartbeat: cron job %s created (%s, deliver %s)" % (job["id"], SCHEDULE, fields["deliver"])
    update_job(job["id"], {**fields, "schedule": parse_schedule(SCHEDULE), "enabled": True,
                           "state": "scheduled", "paused_at": None, "paused_reason": None})
    return "heartbeat: cron job %s updated (%s, deliver %s)" % (job["id"], SCHEDULE, fields["deliver"])


def status() -> str:
    job = _find_job()
    result = probe.check()
    if job is None:
        state = "not installed"
    elif job.get("script") != SCRIPT:
        state = "conflict - job %s named %r runs something else" % (job["id"], JOB_NAME)
    else:
        state = "installed (%s, deliver %s)" % (job["id"], job.get("deliver"))
    lines = ["cron job: %s" % state,
             "stock contract: %s" % ("ok" if result.warning is None else result.warning)]
    return "\n".join(lines)
