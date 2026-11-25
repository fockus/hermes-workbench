#!/usr/bin/env python3
"""Package-only inspection, or install selected registrations through the pinned Hermes PM."""

import argparse
import base64
from collections import Counter
import hashlib
import io
import json
import os
import platform
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
CAT = json.loads((HERE / "catalog.json").read_text())
ROWS = {r["name"]: r for r in CAT["registrations"]}
DEFAULTS = json.loads((HERE / "profile.defaults.json").read_text())["plugins"]
FILES = json.loads((HERE / "files.sha256.json").read_text())
BASE = runpy.run_path(str(HERE.parent / "install.py"))
NPM = "https://registry.npmjs.org/@tintinweb/pi-subagents/-/pi-subagents-0.23.0.tgz"
INTEGRITY = "yidcaZqUjOOM+s++rGaBE17tjxD6b0ROfFrgdqbyaJINMdrAZUuErqT2Sx3NGAfA70jl+jvEqdKeU/IqhGUSPA=="
PROGRESS_SHA = "a180b50a02c68df4bc6d5309e7616ab7cac3e27a9d45b181fe96fdff39f70b4b"
RTK_ASSET = "rtk-oss-aarch64-apple-darwin.tar.gz"
RTK_SHA = "1959bd6d89a729ccdee176b7141490bbe750820f032c74f29a752576f2495254"
GIT = [
    "/usr/bin/git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "commit.gpgSign=false",
]


def need(ok, error):
    if not ok:
        raise ValueError(error)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    need(
        len(ROWS) == len(CAT["registrations"]) == 92,
        "inventory count/uniqueness differs",
    )
    need(CAT["host"]["commit"] == BASE["LOCK"]["commit"], "different host lock")
    for directory in ("sources", "services", "base-overlays", "notices"):
        files = {
            p.relative_to(HERE).as_posix()
            for p in (HERE / directory).rglob("*")
            if not p.is_dir()
        }
        need(
            files == {p for p in FILES if p.startswith(directory + "/")},
            f"unexpected or missing files in {directory}",
        )
    for name, digest in FILES.items():
        p = HERE / name
        need(
            p == p.resolve() and p.is_file() and sha(p) == digest,
            f"packaged content differs: {name}",
        )
    for r in ROWS.values():
        if r["channel"] == "upstream":
            need(
                re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", r["url"])
                and re.fullmatch("[0-9a-f]{40}", r["commit"]),
                "invalid source pin",
            )
        if r["channel"] != "host":
            n = r["name"]
            expected = (
                "enabled"
                if n in DEFAULTS["enabled"]
                else "disabled"
                if n in DEFAULTS["disabled"]
                else "not-enabled"
            )
            need(r["state"] == expected, "state differs: " + n)
    return dict(Counter(r["channel"] for r in ROWS.values()))


def run(args, env, *, cwd, output=False, interactive=False):
    return subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        env=env,
        check=True,
        stdin=None if interactive else subprocess.DEVNULL,
        stdout=subprocess.PIPE if output else None,
        text=True,
        timeout=600,
    ).stdout


def safe(p):
    need(p.absolute() == p.resolve(), "refusing symlink or ambiguous path: " + str(p))
    return p


def context(prefix, layout, *, installing=False):
    p = BASE["target"](prefix, None)
    safe(Path(prefix).expanduser().absolute())
    rec = safe(p / BASE["RECEIPT"])
    data = json.loads(rec.read_text())
    need(data["mode"] in ("base", "tui"), "install Workbench base or tui first")
    integrated = data.get('full_profile', False)
    layout = layout or ('integrated' if integrated else 'pack')
    need((layout == 'integrated') == integrated, 'different receipt layout; choose the matching profile')
    BASE["verify_existing"](p, data["mode"], bool(data.get("executors")), full=integrated,
                            preparing=installing and (p / BASE['PENDING']).is_file())
    for rel in (
        "source",
        "tool-bin",
        "tmp",
        "home",
        "profile",
        "profile/config.yaml",
        "venv/bin/hermes",
        "plugin-pack",
    ):
        safe(p / rel)
    shared = p / "plugin-pack"
    profile = shared / 'profile' if layout == 'pack' else p / 'profile'
    home = shared / 'home' if layout == 'pack' else p / 'home'
    for d in (shared, profile, home):
        safe(d).mkdir(mode=0o700, exist_ok=True)
    for obj in ("plugins", "config.yaml", "plugins/.install-metadata.json"):
        safe(profile / obj)
    env = BASE["environment"](home, p / "tmp")
    env.update(
        PATH=f"{p}/venv/bin:{p}/tool-bin:/usr/bin:/bin",
        HERMES_HOME=str(profile),
        VIRTUAL_ENV=str(p / "venv"),
        HERMES_PYTHON=str(p / "venv/bin/python"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        LANG="en_US.UTF-8",
        WORKBENCH_MB_RUNTIME=str(shared / "runtime/memory-bank"),
        HERMES_NO_UPDATE_CHECK="1",
    )
    if (p / "tool-bin/node").is_file():
        env.update(HERMES_NODE=str(p / "tool-bin/node"), HERMES_TUI_DIR=str(p / "tui"))
    head = run(
        [*GIT, "-C", p / "source", "rev-parse", "HEAD"], env, cwd=p, output=True
    ).strip()
    need(head == CAT["host"]["commit"], "different installed host revision")
    for row in list(ROWS.values()) + CAT["separate"]["provider-plugin"]:
        if row.get("channel") == "host":
            f = safe(p / "source" / row["manifest"])
            blob = hashlib.sha1(
                b"blob " + str(f.stat().st_size).encode() + b"\0" + f.read_bytes()
            ).hexdigest()
            need(blob == row["blob"], "different upstream manifest: " + row["name"])
    return p, shared, profile, env, layout


def pkg_progress(path, env):
    # No npm lifecycle/installer; read precisely one reviewed path from an integrity-locked tarball.
    if path:
        raw = Path(path).read_bytes()
    else:
        raw = subprocess.check_output(
            [
                "/usr/bin/curl",
                "-qfsSL",
                "--max-time",
                "90",
                "--max-filesize",
                "20000000",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                NPM,
            ],
            env=env,
        )
    need(
        base64.b64encode(hashlib.sha512(raw).digest()).decode() == INTEGRITY,
        "npm tarball integrity failure",
    )
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        nodes = [m for m in tf if m.name == "package/dist/workflow/progress.js"]
        need(
            len(nodes) == 1 and nodes[0].isreg() and nodes[0].size < 200000,
            "npm member differs",
        )
        b = tf.extractfile(nodes[0]).read()
        need(hashlib.sha256(b).hexdigest() == PROGRESS_SHA, "progress.js differs")
        return b


def assemble(r, p, shared, env, archive):
    name = r["name"]
    repo = safe(shared / "packages" / name)
    safe(repo.parent).mkdir(mode=0o700, exist_ok=True)
    # Build on every invocation: the commit is a content pin; never trust existing mutable worktree files.
    with tempfile.TemporaryDirectory(dir=p / "tmp") as td:
        stage = Path(td) / "source"
        source = HERE / r["path"]
        src = source / "plugin" if r["channel"] == "base-overlay" else source
        shutil.copytree(src, stage)
        if r["channel"] == "base-overlay":
            shutil.copyfile(source / "LICENSE", stage / "LICENSE")
        if r["channel"] == "packaged":
            shutil.copyfile(HERE / "sources/NOTICE.txt", stage / "WORKBENCH-NOTICE.txt")
        if name == "codemode":
            shutil.copyfile(
                HERE.parent / "LICENSES/Hermes-Agent.txt",
                stage / "patch/Hermes-Agent-LICENSE.txt",
            )
        if name == "workflows":
            (stage / "vendor/pi-subagents/progress.js").write_bytes(
                pkg_progress(archive, env)
            )
        if name == "memory-bank":
            for filename, key in [
                ("__init__.py", "SKILL_ROOT"),
                ("team_guard.py", "REPO_SKILL_ROOT"),
            ]:
                f = stage / filename
                text = f.read_text()
                old = key + " = Path(__file__).resolve().parents[2]"
                need(text.count(old) == 1, "runtime binding changed")
                f.write_text(
                    text.replace(
                        old,
                        key
                        + ' = Path(os.environ["WORKBENCH_MB_RUNTIME"]).resolve(strict=True)',
                    )
                )
        # Stable modes and metadata: repository contains only the complete plugin, not any user's Git history.
        for f in stage.rglob("*"):
            if f.is_file():
                f.chmod(0o644)
        git_env = dict(
            env,
            GIT_AUTHOR_NAME="Workbench package",
            GIT_COMMITTER_NAME="Workbench package",
            GIT_AUTHOR_EMAIL="pack@workbench.invalid",
            GIT_COMMITTER_EMAIL="pack@workbench.invalid",
            GIT_AUTHOR_DATE="2000-01-01T00:00:00Z",
            GIT_COMMITTER_DATE="2000-01-01T00:00:00Z",
        )
        for cmd in (
            ["init", "--quiet", "--template="],
            ["add", "--all"],
            ["commit", "--quiet", "-m", name],
        ):
            run([*GIT, *cmd], git_env, cwd=stage)
        rev = run([*GIT, "rev-parse", "HEAD"], git_env, cwd=stage, output=True).strip()
        if repo.exists():
            need(
                run([*GIT, "rev-parse", "HEAD"], git_env, cwd=repo, output=True).strip()
                == rev,
                "pack repository drift: " + name,
            )
        else:
            stage.rename(repo)
    return repo.as_uri(), rev


def runtimes(p, store):
    dest = safe(store / "runtime")
    need(not dest.exists(), "runtime already present: refusing replacement")
    with tempfile.TemporaryDirectory(dir=p / "tmp") as td:
        stage = Path(td) / "runtime"
        stage.mkdir()
        mb = stage / "memory-bank"
        wk = stage / "llm-wiki"
        # Base verifier checked the exact file set, modes, SHA and absence of symlinks. Not a profile or data store.
        shutil.copytree(p / "memory-bank", mb)
        shutil.copytree(p / "llm-wiki-src", wk)
        changes = HERE / "base-overlays/memory-bank"
        shutil.copytree(changes / "runtime-changes", mb, dirs_exist_ok=True)
        shutil.copytree(HERE / "base-overlays/llm-wiki/runtime-changes", wk, dirs_exist_ok=True)
        shutil.copytree(
            changes / "plugin", mb / "hermes/memory-bank", dirs_exist_ok=True
        )
        (wk / ".venv").symlink_to(p / "wiki-venv", target_is_directory=True)
        shutil.copytree(HERE / "services/recall", stage / "recall")
        shutil.copyfile(
            HERE / "sources/NOTICE.txt", stage / "recall/WORKBENCH-NOTICE.txt"
        )
        stage.rename(dest)
    print(
        "Pack runtimes prepared. No database, service, model, scheduled job or credentials installed."
    )


def config(profile, p, env):
    # Read selected state only. Do not import Hermes or a plugin to audit its enabled flag.
    code = "import sys,json,yaml,pathlib;p=pathlib.Path(sys.argv[1]);print(json.dumps((yaml.safe_load(p.read_text()) or {}).get('plugins',{}) if p.is_file() else {}))"
    return json.loads(
        run(
            [p / "venv/bin/python", "-I", "-B", "-c", code, profile / "config.yaml"],
            env,
            cwd=p,
            output=True,
        )
    )


def status(p, profile, env):
    state = config(profile, p, env)
    en = set(state.get("enabled", []))
    dis = set(state.get("disabled", []))
    gaps = []
    for r in ROWS.values():
        name = r["name"]
        desired = r["state"]
        if r["channel"] == "host":
            if (
                name in dis
                and desired == "enabled"
                or name in en
                and desired == "not-enabled"
            ):
                gaps.append([name, "host state conflict"])
            continue
        where = profile / "plugins" / name
        if not where.is_dir():
            gaps.append([name, "missing source"])
            continue
        actual = (
            "disabled" if name in dis else "enabled" if name in en else "not-enabled"
        )
        if actual != desired or name in en and name in dis:
            gaps.append([name, actual + " != " + desired])
    print(json.dumps({"state_differences": gaps, "runtime_verified": False}, indent=2))
    return int(bool(gaps))


def desktop(names, p, profile, env):
    halves = {r["name"]: r for r in CAT["separate"]["desktop-plugin"]}
    names = list(halves) if names == ["all"] else names
    need(
        names and set(names) <= halves.keys(),
        "choose a listed Desktop component or all",
    )
    script = """import sys,json
from pathlib import Path
from hermes_constants import get_default_hermes_root
from hermes_cli.desktop_plugin_install import install_desktop_plugin
url,rev,catalog,root,name=sys.argv[1:];home=get_default_hermes_root().resolve();prefix=Path(root)
if not home.is_relative_to(prefix) or (home/'desktop-plugins').resolve()!=home/'desktop-plugins': raise SystemExit('outside isolated prefix')
result=install_desktop_plugin(url,ref=rev,catalog_name=catalog,force=False)
if not result.get('ok') or result.get('sha')!=rev or result.get('plugin_name')!=name: raise SystemExit(str(result.get('error','unexpected Desktop receipt')))
print(json.dumps(result))"""
    for n in names:
        r = ROWS[n]
        url = r["url"] + ("#" + r["subdir"] if r["subdir"] != "." else "")
        run(
            [
                p / "venv/bin/python",
                "-I",
                "-B",
                "-c",
                script,
                url,
                r["commit"],
                r["catalog"],
                p,
                n,
            ],
            env,
            cwd=Path(env["HOME"]),
        )


def deps(n, p, shared, profile, env):
    if n == "codemode":
        directory = safe(profile / "plugins/codemode")
        lock = safe(directory / "requirements.lock")
        need(
            sha(lock) == FILES["sources/codemode/requirements.lock"],
            "install pinned CodeMode first",
        )
        parent = safe(directory / ".deps")
        parent.mkdir(mode=0o700, exist_ok=True)
        dest = safe(parent / "cp314")
        need(not dest.exists(), "existing Monty target; refusing replacement")
        uv = shutil.which("uv")
        need(uv, "Workbench uv required")
        need(
            run([uv, "--version"], env, cwd=p, output=True).strip().split()[1]
            == BASE["LOCK"]["uv"],
            "different uv version",
        )
        with tempfile.TemporaryDirectory(dir=p / "tmp") as td:
            stage = Path(td) / "wheels"
            run(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    p / "venv/bin/python",
                    "--target",
                    stage,
                    "--require-hashes",
                    "--no-deps",
                    "--only-binary",
                    ":all:",
                    "-r",
                    lock,
                ],
                env,
                cwd=p,
            )
            stage.rename(dest)
    elif n == "rtk-rewrite":
        need(
            (platform.system(), platform.machine()) == ("Darwin", "arm64"),
            "only the pinned macOS arm64 RTK archive is provided",
        )
        dest = safe(p / "tool-bin/rtk")
        record = safe(shared / "rtk-binary.sha256")
        need(
            not dest.exists() and not record.exists(),
            "existing RTK; refusing replacement",
        )
        with tempfile.TemporaryDirectory(dir=p / "tmp") as td:
            archive = Path(td) / RTK_ASSET
            url = "https://github.com/rtk-ai/rtk/releases/download/v1.2.0/" + RTK_ASSET
            run(
                [
                    "/usr/bin/curl",
                    "-qfsSL",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--max-time",
                    "120",
                    "--output",
                    archive,
                    url,
                ],
                env,
                cwd=p,
            )
            need(sha(archive) == RTK_SHA, "RTK release checksum differs")
            with tarfile.open(archive, "r:gz") as tf:
                members = [
                    m
                    for m in tf.getmembers()
                    if m.isfile() and Path(m.name).name in ("rtk", "rtk-oss")
                ]
                need(len(members) == 1, "ambiguous binary")
                stage = Path(td) / "rtk"
                stage.write_bytes(tf.extractfile(members[0]).read())
                stage.chmod(0o755)
                notice = safe(shared / "rtk-notices")
                notice.mkdir(mode=0o700)
                for i, m in enumerate(tf.getmembers()):
                    if m.isfile() and Path(m.name).name.upper().startswith(
                        ("LICENSE", "NOTICE", "COPYING")
                    ):
                        (notice / str(i)).write_bytes(tf.extractfile(m).read())
                record.write_text(sha(stage) + "\n")
                os.link(stage, dest)
    else:
        raise ValueError(
            "deps supports codemode and rtk-rewrite; use native Hermes PM consent for other declared dependencies"
        )


def source_id(url):
    repo, sep, subdir = url.partition('#')
    return repo.removesuffix('.git') + sep + subdir


def one(r, p, store, profile, env, cmd, args):
    n = r["name"]
    chan = r["channel"]
    state = r["state"]
    hermes = [p / "venv/bin/hermes"]

    def runhost(*arguments):
        return run([*hermes, *arguments], env, cwd=Path(env["HOME"]))

    if chan == "host":
        print(f"{n}: bundled ({state}); no override")
        return
    if chan == "base-overlay" and args.profile == "base":
        raise ValueError(
            f"{n}: original differs from the receipt-managed base adapter. Use --profile pack (new pack profile), or integrate the overlay in the base build. Base not overwritten."
        )
    dest = safe(profile / "plugins" / n)
    meta = safe(profile / "plugins/.install-metadata.json")
    if chan == "upstream":
        url = r["url"] + ("#" + r["subdir"] if r["subdir"] != "." else "")
        rev = r["commit"]
    else:
        url, rev = assemble(r, p, store, env, args.workflows_archive)
    if not dest.exists():
        need(cmd == "install", "first install " + n)
        runhost(
            "plugins",
            "install",
            r.get("catalog") or url,
            "--ref",
            rev,
            "--no-enable",
            "--no-deps",
        )
    m = json.loads(meta.read_text()).get(n, {}) if meta.is_file() else {}
    need(
        m.get("revision") == rev
        and source_id(m.get('source', '')) == source_id(url)
        and dest.is_dir(),
        "missing or mismatched host install receipt: " + n,
    )
    c = config(profile, p, env)
    en = c.get("enabled", [])
    dis = c.get("disabled", [])
    settings = dict(DEFAULTS["entries"].get(n, {}))
    if n == "llm-wiki":
        settings = {"settings": {"runtime_root": str(store / "runtime/llm-wiki")}}
    if settings:
        existing = c.get("entries", {}).get(n, {})
        # Never replace an operator's other keys (including host-managed grants).
        for key, value in settings.items():
            need(
                key not in existing or existing[key] == value,
                "existing settings differ; refusing to overwrite " + n + "." + key,
            )
            if key not in existing:
                runhost(
                    "config",
                    "set",
                    "plugins.entries." + n + "." + key,
                    json.dumps(value),
                )
    if cmd == "install":
        if state == "disabled" and n not in dis:
            runhost("plugins", "disable", n)
        if state == "disabled":
            need(
                n in config(profile, p, env).get("disabled", []),
                "host did not persist disable: " + n,
            )
        if state == "not-enabled":
            need(
                n not in en and n not in dis,
                "existing explicit setting conflicts with not-enabled: " + n,
            )
        print(n + ": source installed; desired " + state)
        return
    need(
        state == "enabled", "refusing to activate a baseline " + state + " plugin: " + n
    )
    if n == "codemode":
        need(
            (dest / ".deps/cp314").is_dir(),
            "prepare the verified Monty dependency before enabling; see README",
        )
    if n == "rtk-rewrite":
        need(
            (store / "rtk-binary.sha256").is_file()
            and sha(safe(p / "tool-bin/rtk"))
            == (store / "rtk-binary.sha256").read_text().strip(),
            "supply pinned RTK first; see README",
        )
    if n in ("llm-wiki", "memory-bank"):
        need((store / "runtime" / n).is_dir(), "prepare-runtimes first")
    if n not in en:
        run(
            [*hermes, "plugins", "enable", n, "--no-allow-tool-override"],
            env,
            cwd=Path(env["HOME"]),
            interactive=True,
        )
    final = config(profile, p, env)
    need(
        n in final.get("enabled", []) and n not in final.get("disabled", []),
        "host did not admit/enable " + n,
    )
    print(n + ": enabled in config (not a functionality test)")


def main():
    argv = sys.argv[1:]
    pos = argv.index("--") if "--" in argv else len(argv)
    forwarded = argv[pos + 1 :]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "command",
        choices=[
            "inventory",
            "prepare-runtimes",
            "install",
            "enable",
            "run",
            "desktop-install",
            "deps",
            "status",
        ],
    )
    ap.add_argument("names", nargs="*")
    ap.add_argument("--prefix")
    ap.add_argument("--profile", choices=["base", "pack", "integrated"], help='default: layout from Workbench receipt')
    ap.add_argument("--workflows-archive", help="optional pinned upstream npm tarball")
    ap.add_argument("--accept-activation", action="store_true")
    a = ap.parse_intermixed_args(argv[:pos])
    stats = verify()
    need(not forwarded or a.command == "run", "trailing arguments are for run only")
    if a.command == "inventory":
        print(
            json.dumps(
                {
                    "registrations": stats,
                    "other_surfaces": {k: len(v) for k, v in CAT["separate"].items()},
                },
                indent=2,
            )
        )
        return 0
    need(a.prefix, "--prefix must identify an existing isolated Workbench")
    p, store, profile, env, a.profile = context(a.prefix, a.profile, installing=a.command == 'install')
    if a.command == "status":
        return status(p, profile, env)
    if a.command == "prepare-runtimes":
        runtimes(p, store)
        return 0
    if a.command == "deps":
        need(len(a.names) == 1, "choose one dependency")
        deps(a.names[0], p, store, profile, env)
        return 0
    if a.command == "desktop-install":
        if a.profile == 'integrated':
            need(a.names != ['all'] and not (set(a.names) & set(BASE['PROFILE_HOLDBACK'])),
                 'choose specific non-held Desktop names in the integrated profile')
        need(
            a.accept_activation,
            "Desktop may load packages on its next scan; pass --accept-activation",
        )
        desktop(a.names, p, profile, env)
        return 0
    if a.command == "run":
        need(not a.names, "put Hermes arguments after --")
        os.chdir(env["HOME"])
        exe = p / "venv/bin/hermes"
        os.execve(exe, [str(exe), *forwarded], env)
    need(a.names, "specify plugin names or all")
    need(
        a.command != "enable" or a.accept_activation,
        "enable requires explicit --accept-activation",
    )
    names = list(ROWS) if a.names == ["all"] else a.names
    need(
        len(set(names)) == len(names) and set(names) <= ROWS.keys(),
        "duplicate or unknown plugin name",
    )
    fail = []
    for n in names:
        if a.profile == 'integrated' and n in BASE['PROFILE_HOLDBACK']:
            if a.names != ['all']:
                raise ValueError(f'{n}: held outside integrated profile: ' + BASE['PROFILE_HOLDBACK'][n])
            print(f'{n}: EXCLUDED — ' + BASE['PROFILE_HOLDBACK'][n], file=sys.stderr)
            continue
        if (
            a.command == "enable"
            and a.names == ["all"]
            and ROWS[n]["state"] != "enabled"
        ):
            continue
        try:
            one(ROWS[n], p, store, profile, env, a.command, a)
        except (OSError, ValueError, subprocess.SubprocessError) as ex:
            fail.append(n)
            print(n + ": BLOCKED: " + str(ex), file=sys.stderr)
    if fail:
        print("Not completed: " + ", ".join(fail), file=sys.stderr)
        return 1
    if a.profile == 'integrated' and a.command == 'install' and (p/BASE['PENDING']).exists():
        state = config(profile, p, env)
        expected_disabled = {n for n in names if ROWS[n]['state'] == 'disabled'}
        need(not state.get('enabled') and expected_disabled <= set(state.get('disabled', [])),
             'unexpected initial activation/disable state; no completed install receipt')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        sys.exit(str(e))
