"""Packaging and external payload volume regression entry point.

Runs the original installed-artifact baseline, then the external volume
acceptance: ready-state reconciliation on a real mounted volume, the full
mount/unmount cycle matrix when the platform volume lab can really attach
volumes (macOS hdiutil images, or privileged Linux bind mounts), and the real
LaunchAgent go-live when a macOS GUI login session exists. Checks the
environment cannot perform are recorded as NOT_EXECUTED with evidence; they
are never simulated by renaming directories or forging identities.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

from agentcheck import inspect
from checks import assert_results, fixtures
from pipeline import digest
from volguard import mounted_identity, mountpoint_of

MACOS_NOISE = {".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems",
               ".DS_Store", ".metadata_never_index"}


class Report:
    def __init__(self):
        self.items = []

    def record(self, name, status, detail=""):
        self.items.append({"name": name, "status": status, "detail": detail})
        note = ""
        if status != "PASS" and detail.strip():
            note = " - " + detail.strip().splitlines()[-1][:400]
        print("%s: %s%s" % (status, name, note), file=sys.stderr, flush=True)


def run_check(report, name, func, *args):
    try:
        func(*args)
    except Exception:
        report.record(name, "FAIL", traceback.format_exc())
        return False
    report.record(name, "PASS")
    return True


def call_json(argv, work, env, success=True, extra_env=None, timeout=60):
    result = subprocess.run(argv, cwd=str(work), env=extra_env or env,
                            capture_output=True, text=True, timeout=timeout)
    if not success:
        assert result.returncode != 0, (argv, result.stdout)
        return result
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return json.loads(result.stdout)


def pause_at(command, env, work, gate, event, timeout=30):
    gate = Path(gate)
    child_env = dict(env, POINTWISE_PAUSE=event, POINTWISE_GATE=str(gate))
    child = subprocess.Popen(command, cwd=str(work), env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    while not (gate / "ready.json").exists():
        assert child.poll() is None, child.communicate()
        if time.monotonic() > deadline:
            child.kill()
            child.communicate()
            raise AssertionError("child did not reach %s" % event)
        time.sleep(0.02)
    return child


def tree_snapshot(root):
    root = Path(root)
    snap = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in MACOS_NOISE for part in rel.parts):
            continue
        if path.is_file():
            snap[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


def bare_entries(mount):
    return [name for name in os.listdir(mount) if name not in MACOS_NOISE]


def make_batch(name, segments=1):
    return {"batch": name, "segments": [
        [{"id": "%s-%d" % (name, i), "rev": 1, "sensor": "s-%s" % name,
          "ts": 61 * (i + 1), "value": i + 1, "deleted": False}]
        for i in range(segments)]}


def expected_rows(name, segments=1):
    return [{"sensor": "s-%s" % name, "minute": i + 1, "sum": i + 1, "count": 1}
            for i in range(segments)]


def assert_batch(read_result, batch, segments=1):
    items = [item for item in read_result if item["batch"] == batch["batch"]]
    assert len(items) == 1, (batch["batch"], read_result)
    assert items[0]["rows"] == expected_rows(batch["batch"], segments), items[0]
    assert items[0]["receipt"] == {"batch": batch["batch"], "sha256": digest(batch),
                                   "groups": segments, "samples": segments}, items[0]


class Ctx:
    def __init__(self, python, app, root, env, work):
        self.python, self.app, self.root = python, str(app), str(root)
        self.env, self.work = env, str(work)

    def command(self, *args):
        return [self.python, self.app] + [str(arg) for arg in args]

    def call(self, argv, success=True, extra_env=None, timeout=60):
        return call_json(argv, self.work, self.env, success, extra_env, timeout)

    def run(self, *args, **kwargs):
        return self.call(self.command(*args), **kwargs)

    def submit(self, batch, area, **kwargs):
        path = Path(area) / ("submit-%s.json" % batch["batch"])
        path.write_text(json.dumps(batch), encoding="utf-8")
        return self.run("submit", self.root, path, **kwargs)

    def drain(self, limit=256):
        for _ in range(limit):
            if not self.run("status", self.root)["pending"]:
                return
            self.run("run", self.root, "--once")
        raise AssertionError("drain did not finish")

    def read(self):
        return self.run("read", self.root)

    def status(self):
        return self.run("status", self.root)


def baseline(evidence, base, python, env, children):
    with tempfile.TemporaryDirectory(prefix="换卷 release ") as temporary:
        area = Path(temporary).resolve()
        source, package, dest = area / "source", area / "package", area / "安装目录"
        source.mkdir()
        for path in base.glob("*.py"):
            shutil.copy2(path, source / path.name)
        work = area / "empty"
        work.mkdir()

        def call(argv, success=True, extra_env=None):
            return call_json(argv, work, env, success, extra_env, timeout=30)

        call([python, str(source / "ops.py"), "build", str(package)])
        second = area / "second"
        call([python, str(source / "ops.py"), "build", str(second)])
        assert (package / "app.pyz").read_bytes() == (second / "app.pyz").read_bytes()
        info = call([python, str(source / "ops.py"), "install", str(package), str(dest),
                     "--python", python, "--no-load"])
        app, root = Path(info["app"]), Path(info["root"])
        assert app == dest / "app.pyz" and root == dest / "data"
        assert inspect(info["plist"], app, root, python) == info["label"]
        shutil.rmtree(source)

        def command(*args):
            return [python, str(app)] + [str(arg) for arg in args]

        def run(*args):
            return call(command(*args))

        def submit(batch):
            path = area / (batch["batch"] + ".json")
            path.write_text(json.dumps(batch), encoding="utf-8")
            return run("submit", root, path)

        def finish():
            for _ in range(128):
                if not run("status", root)["pending"]:
                    return
                run("run", root, "--once")
            raise AssertionError("fixture did not complete")

        before = app.read_bytes(), Path(info["plist"]).read_bytes()
        broken = area / "broken"
        shutil.copytree(package, broken)
        with (broken / "app.pyz").open("ab") as out:
            out.write(b"corrupt")
        call(command("install", broken, dest, "--python", python, "--no-load"), False)
        assert before == (app.read_bytes(), Path(info["plist"]).read_bytes())
        a, b = fixtures()
        ack = submit(a)
        submit(b)
        gate = area / "gate"
        child_env = dict(env, POINTWISE_PAUSE="checkpoint_loaded", POINTWISE_GATE=str(gate))
        child = subprocess.Popen(command("run", root, "--once"), cwd=str(work), env=child_env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        deadline = time.monotonic() + 20
        while not (gate / "ready.json").exists():
            assert child.poll() is None, child.communicate()
            if time.monotonic() > deadline:
                raise AssertionError("worker did not reach checkpoint_loaded")
            time.sleep(0.02)
        call(command("run", root, "--once"), False)
        assert run("read", root) == []
        assert submit(a) == ack
        (gate / "continue").touch()
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, (stdout, stderr)
        finish()
        result = run("read", root)
        assert_results(result)
        assert submit(a) == ack
        assert run("run", root, "--once") == {"progressed": False}
        evidence["installed_baseline"] = {
            "results": result, "acceptance": ack, "status": run("status", root),
            "reproducible": True, "detached_source": True,
            "agent": "configuration-only here; live loading covered by live_launch_agent"}


def pick_ready_volume():
    candidates = []
    override = os.environ.get("POINTWISE_READY_VOLUME")
    if override:
        candidates.append(override)
    candidates.extend(["/dev/shm", "/workspace"])
    for candidate in candidates:
        try:
            if not os.path.isdir(candidate) or not os.access(candidate, os.W_OK | os.X_OK):
                continue
            mountpoint = mountpoint_of(candidate)
            if mountpoint and mountpoint != "/":
                return candidate
        except Exception:
            continue
    return None


def external_ready(ctx_box, evidence, base, python, env, source, package, dest, work, payload):
    call_json([python, str(source / "ops.py"), "build", str(package)], work, env)
    info = call_json([python, str(source / "ops.py"), "install", str(package), str(dest),
                      "--python", python, "--no-load", "--payload-dir", str(payload)], work, env)
    app, root = Path(info["app"]), Path(info["root"])
    assert app == dest / "app.pyz" and root == dest / "data"
    binding = json.loads((root / "volume.json").read_text(encoding="utf-8"))
    assert binding["payload_dir"] == str(payload)
    assert binding["mountpoint"] and binding["mountpoint"] != "/"
    assert binding["identity"] == mounted_identity(binding["mountpoint"])
    assert inspect(info["plist"], app, root, python) == info["label"]
    shutil.rmtree(source)
    ctx = Ctx(python, app, root, env, work)
    ctx_box.update(ctx=ctx, root=root, payload=payload, area=dest.parent,
                   work=work, info=info)
    a, b = fixtures()
    ack_a = ctx.submit(a, dest.parent)
    assert ack_a == {"batch": "a", "sha256": digest(a)}
    ctx.submit(b, dest.parent)
    assert ctx.submit(a, dest.parent) == ack_a
    status = ctx.status()
    assert status["volume_state"] == "ready", status
    assert len(status["pending"]) == 2 and status["inventory"], status
    ctx.drain()
    result = ctx.read()
    assert_results(result)
    assert ctx.submit(a, dest.parent) == ack_a
    changed = copy.deepcopy(a)
    changed["segments"][0][0]["value"] += 1
    conflict = ctx.submit(changed, dest.parent, success=False)
    assert "BATCH_CONFLICT" in conflict.stderr
    status = ctx.status()
    assert status["volume_state"] == "ready" and status["required"] == [], status
    assert not (root / "blobs").exists(), "substitute payload directory must not exist"
    ctx_box["acks"] = {"a": ack_a}
    evidence["external_ready_state"] = {
        "read": result, "status": status, "acceptance": ack_a, "binding": binding,
        "detached_source": True}


def external_kill(ctx_box, evidence, env, children):
    ctx, area = ctx_box["ctx"], ctx_box["area"]
    c = make_batch("c", 2)
    ack_c = ctx.submit(c, area)
    gate = area / "gate-volume-checked"
    child = pause_at(ctx.command("run", ctx.root, "--once"), env, ctx.work, gate, "volume_checked")
    children.append(child)
    ready = json.loads((gate / "ready.json").read_text(encoding="utf-8"))
    assert ready["event"] == "volume_checked"
    assert ready["detail"]["operation"] in ("get", "put"), ready
    child.kill()
    child.communicate(timeout=30)
    shutil.rmtree(gate)
    gate = area / "gate-before-publish"
    child = pause_at(ctx.command("run", ctx.root, "--once"), env, ctx.work, gate, "before_object_publish")
    children.append(child)
    child.kill()
    child.communicate(timeout=30)
    shutil.rmtree(gate)
    ctx.drain()
    result = ctx.read()
    assert_batch(result, c, 2)
    assert ctx.submit(c, area) == ack_c, "killed and resumed batch must not double-account"
    ctx_box["acks"]["c"] = ack_c
    ctx_box["c_batch"] = c
    evidence["external_kill_restart"] = {"read": result, "acceptance_c": ack_c}


def external_daemon(ctx_box, evidence, env, children):
    ctx, area = ctx_box["ctx"], ctx_box["area"]
    d, e = make_batch("d"), make_batch("e")
    ack_d = ctx.submit(d, area)
    daemon = subprocess.Popen(ctx.command("run", ctx.root), cwd=ctx.work, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    children.append(daemon)
    try:
        deadline = time.monotonic() + 60
        while "d" not in [item["batch"] for item in ctx.read()]:
            assert daemon.poll() is None and time.monotonic() < deadline, "daemon did not drain d"
            time.sleep(0.2)
        busy = ctx.run("run", ctx.root, "--once", success=False)
        assert "WORKER_BUSY" in busy.stderr
        ack_e = ctx.submit(e, area)
        deadline = time.monotonic() + 60
        while "e" not in [item["batch"] for item in ctx.read()]:
            assert daemon.poll() is None and time.monotonic() < deadline, "daemon did not drain e"
            time.sleep(0.2)
    finally:
        daemon.kill()
        daemon.communicate(timeout=30)
    result = ctx.read()
    assert_batch(result, d)
    assert_batch(result, e)
    assert ctx.submit(d, area) == ack_d
    assert ctx.submit(e, area) == ack_e
    evidence["external_daemon"] = {"read": result, "acceptance_d": ack_d}


def external_corrupt(ctx_box, evidence, env):
    ctx, area, payload = ctx_box["ctx"], ctx_box["area"], ctx_box["payload"]
    f = make_batch("f")
    ctx.submit(f, area)
    target = payload / (digest(f["segments"][0]) + ".json")
    assert target.exists(), target
    with target.open("ab") as out:
        out.write(b"corrupt")
    first = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_HASH" in first.stderr, first.stderr
    second = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_HASH" in second.stderr, "corrupt payload must fail deterministically"
    status = ctx.status()
    assert status["volume_state"] == "ready", "data fault must not look like a volume problem"
    target.unlink()
    missing = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_MISSING" in missing.stderr, missing.stderr
    result = ctx.read()
    assert_results([item for item in result if item["batch"] in ("a", "b")])
    assert all(item["batch"] != "f" for item in result)
    evidence["external_corrupt_payload"] = {
        "hash_error": first.stderr.strip(), "missing_error": missing.stderr.strip(),
        "volume_state": status["volume_state"], "completed": len(result)}


def external(report, evidence, base, python, env, children):
    names = ["external_ready_state", "external_kill_restart",
             "external_daemon", "external_corrupt_payload"]
    volume = pick_ready_volume()
    if volume is None:
        for name in names:
            report.record(name, "NOT_EXECUTED", "no writable non-root mounted volume available")
        return
    with tempfile.TemporaryDirectory(prefix="external volume ") as temporary:
        area = Path(temporary).resolve()
        source, package, dest = area / "source", area / "package", area / "安装目录"
        work = area / "empty"
        source.mkdir()
        work.mkdir()
        for path in base.glob("*.py"):
            shutil.copy2(path, source / path.name)
        payload_base = Path(tempfile.mkdtemp(prefix="pointwise-payload-", dir=volume))
        payload = payload_base / "payload"
        payload.mkdir()
        ctx_box = {}
        try:
            ok = run_check(report, names[0], external_ready, ctx_box, evidence,
                           base, python, env, source, package, dest, work, payload)
            if ok:
                run_check(report, names[1], external_kill, ctx_box, evidence, env, children)
                run_check(report, names[2], external_daemon, ctx_box, evidence, env, children)
                run_check(report, names[3], external_corrupt, ctx_box, evidence, env)
            else:
                for name in names[1:]:
                    report.record(name, "NOT_EXECUTED", "ready-state check failed")
        finally:
            shutil.rmtree(payload_base, ignore_errors=True)


def lab_cycle(ctx_box, *args):
    result = subprocess.run(
        [ctx_box["python"], str(ctx_box["base"] / "volumes.py")] + [str(arg) for arg in args],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, (args, result.stderr)
    return json.loads(result.stdout)


def restore_original(ctx_box):
    state = json.loads((Path(ctx_box["lab_area"]) / "volumes.json").read_text(encoding="utf-8"))
    if state["mounted"] != "original":
        if state["mounted"] is not None:
            lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "original")


def matrix_bind(ctx_box, evidence, base, python, env, source, package, dest, work, state, area):
    payload = Path(state["payload"])
    assert payload.is_dir()
    call_json([python, str(source / "ops.py"), "build", str(package)], work, env)
    info = call_json([python, str(source / "ops.py"), "install", str(package), str(dest),
                      "--python", python, "--no-load", "--payload-dir", str(payload)], work, env)
    app, root = Path(info["app"]), Path(info["root"])
    binding = json.loads((root / "volume.json").read_text(encoding="utf-8"))
    assert binding["mountpoint"] == state["mount"], binding
    assert binding["payload_dir"] == state["payload"], binding
    assert binding["identity"] == mounted_identity(state["mount"]), binding
    shutil.rmtree(source)
    ctx = Ctx(python, app, root, env, work)
    a, b = fixtures()
    ack_a = ctx.submit(a, area)
    ctx.submit(b, area)
    ctx.drain()
    assert_results(ctx.read())
    c = make_batch("c", 2)
    ack_c = ctx.submit(c, area)
    status = ctx.status()
    assert status["volume_state"] == "ready" and len(status["pending"]) == 1, status
    ctx_box.update(ctx=ctx, root=root, payload=payload, mount=Path(state["mount"]),
                   area=area, work=work, info=info, base=base, python=python, env=env,
                   acks={"a": ack_a, "c": ack_c}, c_batch=c)
    evidence["matrix_bind"] = {"status": status, "binding": binding, "uuids": state["uuids"]}


def matrix_unavailable(ctx_box, evidence, env):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
    try:
        status = ctx.status()
        assert status["volume_state"] == "volume_unavailable", status
        assert status["inventory"] == {}, status
        assert [item["batch"] for item in status["pending"]] == ["c"], status
        assert status["completed"] == 2, status
        assert_results(ctx.read())
        refused = ctx.submit(make_batch("d"), area, success=False)
        assert "VOLUME_UNAVAILABLE" in refused.stderr, refused.stderr
        assert [item["batch"] for item in ctx.status()["pending"]] == ["c"]
        assert ctx.submit(ctx_box["c_batch"], area) == ctx_box["acks"]["c"]
        once = ctx.run("run", ctx.root, "--once", success=False)
        assert "VOLUME_UNAVAILABLE" in once.stderr, once.stderr
        assert bare_entries(mount) == [], "bare mountpoint must stay empty"
        assert not (ctx_box["root"] / "blobs").exists(), "no substitute payload directory"
        evidence["matrix_volume_unavailable"] = {
            "status": status, "submit_stderr": refused.stderr.strip(),
            "run_once_stderr": once.stderr.strip()}
    finally:
        restore_original(ctx_box)


def matrix_wrong(ctx_box, evidence, env):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
    lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
    sentinel = mount / "sentinel-哨兵.txt"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    (mount / "payload" / "sentinel.json").write_text('{"sentinel": true}', encoding="utf-8")
    snapshot = tree_snapshot(mount)
    try:
        status = ctx.status()
        assert status["volume_state"] == "wrong_volume", status
        assert status["inventory"] == {}, "foreign payloads must not fill inventory"
        refused = ctx.submit(make_batch("d"), area, success=False)
        assert "WRONG_VOLUME" in refused.stderr, refused.stderr
        once = ctx.run("run", ctx.root, "--once", success=False)
        assert "WRONG_VOLUME" in once.stderr, once.stderr
        assert_results(ctx.read())
        assert ctx.submit(ctx_box["c_batch"], area) == ctx_box["acks"]["c"]
        assert tree_snapshot(mount) == snapshot, "foreign volume must stay untouched"
        evidence["matrix_wrong_volume"] = {
            "status": status, "submit_stderr": refused.stderr.strip(),
            "run_once_stderr": once.stderr.strip()}
    finally:
        restore_original(ctx_box)
    ctx.drain()
    assert_batch(ctx.read(), ctx_box["c_batch"], 2)


def matrix_disconnect_checked(ctx_box, evidence, env, children):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    d = make_batch("d", 2)
    ctx.submit(d, area)
    gate = area / "gate-checked"
    child = pause_at(ctx.command("run", ctx.root, "--once"), env, ctx.work, gate, "volume_checked")
    children.append(child)
    try:
        lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        assert bare_entries(mount) == []
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
        snapshot = tree_snapshot(mount)
        (gate / "continue").touch()
        _stdout, stderr = child.communicate(timeout=60)
        assert child.returncode != 0, "worker must not continue on a swapped volume"
        assert "VOLUME_UNAVAILABLE" in stderr or "WRONG_VOLUME" in stderr, stderr
        assert tree_snapshot(mount) == snapshot, "foreign volume must stay untouched"
    finally:
        shutil.rmtree(gate, ignore_errors=True)
        restore_original(ctx_box)
    ctx.drain()
    assert_batch(ctx.read(), d, 2)
    evidence["matrix_disconnect_after_volume_checked"] = {"worker_stderr": stderr.strip()}


def matrix_disconnect_publish(ctx_box, evidence, env, children):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    e = make_batch("e", 2)
    ctx.submit(e, area)
    gate = area / "gate-publish"
    child = pause_at(ctx.command("run", ctx.root, "--once"), env, ctx.work,
                     gate, "before_object_publish")
    children.append(child)
    try:
        lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        assert bare_entries(mount) == []
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
        snapshot = tree_snapshot(mount)
        (gate / "continue").touch()
        _stdout, stderr = child.communicate(timeout=60)
        outcome = "failed"
        if child.returncode == 0:
            # The pinned descriptor still published onto the detached original
            # volume; nothing may have reached the foreign volume either way.
            outcome = "completed_on_detached_original"
        else:
            assert "VOLUME_UNAVAILABLE" in stderr or "WRONG_VOLUME" in stderr, stderr
        assert tree_snapshot(mount) == snapshot, "foreign volume must stay untouched"
    finally:
        shutil.rmtree(gate, ignore_errors=True)
        restore_original(ctx_box)
    ctx.drain()
    assert_batch(ctx.read(), e, 2)
    evidence["matrix_disconnect_before_publish"] = {"worker_stderr": stderr.strip(),
                                                    "outcome": outcome}


def matrix_disconnect_commit(ctx_box, evidence, env, children):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    f = make_batch("f", 2)
    ctx.submit(f, area)
    gate = area / "gate-commit"
    child = pause_at(ctx.command("run", ctx.root, "--once"), env, ctx.work, gate, "before_checkpoint")
    children.append(child)
    try:
        lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        assert bare_entries(mount) == []
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
        snapshot = tree_snapshot(mount)
        (gate / "continue").touch()
        stdout, stderr = child.communicate(timeout=60)
        # The committed reference is backed by an object fully published on
        # the original volume before the pause, so committing is legitimate.
        assert child.returncode == 0, (stdout, stderr)
        assert tree_snapshot(mount) == snapshot, "foreign volume must stay untouched"
    finally:
        shutil.rmtree(gate, ignore_errors=True)
        restore_original(ctx_box)
    status = ctx.status()
    assert set(status["required"]) <= set(status["inventory"]), \
        "committed references must be backed by payloads on the original volume"
    ctx.drain()
    assert_batch(ctx.read(), f, 2)
    evidence["matrix_disconnect_before_reference_commit"] = {"worker_stdout": stdout.strip()}


def matrix_submitter(ctx_box, evidence, env, children):
    ctx, area, mount = ctx_box["ctx"], ctx_box["area"], ctx_box["mount"]
    g = make_batch("g", 2)
    g_path = Path(area) / "submit-g.json"
    g_path.write_text(json.dumps(g), encoding="utf-8")
    gate = area / "gate-submit-put"
    child = pause_at(ctx.command("submit", ctx.root, g_path), env, ctx.work, gate, "volume_checked")
    children.append(child)
    try:
        lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        assert bare_entries(mount) == []
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
        snapshot = tree_snapshot(mount)
        (gate / "continue").touch()
        _stdout, stderr = child.communicate(timeout=60)
        assert child.returncode != 0, "submitter must not accept onto a swapped volume"
        assert "VOLUME_UNAVAILABLE" in stderr or "WRONG_VOLUME" in stderr, stderr
        assert tree_snapshot(mount) == snapshot
        pending = [item["batch"] for item in ctx.status()["pending"]]
        assert "g" not in pending, "no acceptance record may be created"
    finally:
        shutil.rmtree(gate, ignore_errors=True)
        restore_original(ctx_box)
    ack_g = ctx.submit(g, area)
    ctx.drain()
    assert_batch(ctx.read(), g, 2)

    h = make_batch("h", 2)
    h_path = Path(area) / "submit-h.json"
    h_path.write_text(json.dumps(h), encoding="utf-8")
    gate = area / "gate-submit-accept"
    child = pause_at(ctx.command("submit", ctx.root, h_path), env, ctx.work, gate, "before_accept")
    children.append(child)
    try:
        lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
        assert bare_entries(mount) == []
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "foreign")
        snapshot = tree_snapshot(mount)
        (gate / "continue").touch()
        stdout, stderr = child.communicate(timeout=60)
        # Every segment payload was fully published on the original volume
        # before the pause, so the acceptance commit is fully backed.
        assert child.returncode == 0, (stdout, stderr)
        assert json.loads(stdout) == {"batch": "h", "sha256": digest(h)}
        assert tree_snapshot(mount) == snapshot
    finally:
        shutil.rmtree(gate, ignore_errors=True)
        restore_original(ctx_box)
    ctx.drain()
    assert_batch(ctx.read(), h, 2)
    evidence["matrix_submitter_disconnect"] = {"acceptance_g": ack_g}


def matrix_daemon(ctx_box, evidence, env, children):
    ctx, area = ctx_box["ctx"], ctx_box["area"]
    i = make_batch("i")
    ack_i = ctx.submit(i, area)
    lab_cycle(ctx_box, "offline", ctx_box["lab_area"])
    daemon = subprocess.Popen(ctx.command("run", ctx.root), cwd=ctx.work, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    children.append(daemon)
    try:
        time.sleep(3)
        assert daemon.poll() is None, "resident worker must wait, not crash"
        status = ctx.status()
        assert status["volume_state"] == "volume_unavailable", status
        assert "i" in [item["batch"] for item in status["pending"]]
        lab_cycle(ctx_box, "mount", ctx_box["lab_area"], "original")
        deadline = time.monotonic() + 90
        while "i" not in [item["batch"] for item in ctx.read()]:
            assert daemon.poll() is None and time.monotonic() < deadline, \
                "resident worker must resume by itself after remount"
            time.sleep(0.2)
    finally:
        daemon.kill()
        _stdout, stderr = daemon.communicate(timeout=30)
    assert_batch(ctx.read(), i)
    evidence["matrix_daemon_recovery"] = {"acceptance_i": ack_i,
                                          "daemon_stderr": stderr.strip()[-800:]}


def matrix_corrupt(ctx_box, evidence, env):
    ctx, area, payload = ctx_box["ctx"], ctx_box["area"], ctx_box["payload"]
    j = make_batch("j")
    ctx.submit(j, area)
    target = payload / (digest(j["segments"][0]) + ".json")
    assert target.exists(), target
    with target.open("ab") as out:
        out.write(b"corrupt")
    first = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_HASH" in first.stderr, first.stderr
    second = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_HASH" in second.stderr, "corrupt payload must fail deterministically"
    status = ctx.status()
    assert status["volume_state"] == "ready", "data fault must not look like a volume problem"
    target.unlink()
    missing = ctx.run("run", ctx.root, "--once", success=False)
    assert "PAYLOAD_MISSING" in missing.stderr, missing.stderr
    ctx_box["j_oid"] = digest(j["segments"][0])
    evidence["matrix_corrupt_payload"] = {
        "hash_error": first.stderr.strip(), "missing_error": missing.stderr.strip(),
        "volume_state": status["volume_state"]}


def matrix_reconcile(ctx_box, evidence, env):
    ctx, area = ctx_box["ctx"], ctx_box["area"]
    result = ctx.read()
    assert_results([item for item in result if item["batch"] in ("a", "b")])
    for name, segments in (("c", 2), ("d", 2), ("e", 2), ("f", 2), ("g", 2), ("h", 2), ("i", 1)):
        assert_batch(result, make_batch(name, segments), segments)
    assert ctx.submit(ctx_box["c_batch"], area) == ctx_box["acks"]["c"]
    changed = copy.deepcopy(ctx_box["c_batch"])
    changed["segments"][0][0]["value"] += 1
    conflict = ctx.submit(changed, area, success=False)
    assert "BATCH_CONFLICT" in conflict.stderr
    status = ctx.status()
    assert status["volume_state"] == "ready", status
    backed = set(status["required"]) - {ctx_box.get("j_oid")}
    assert backed <= set(status["inventory"]), "references must be backed on the volume"
    assert status["inventory"], status
    assert not (ctx_box["root"] / "blobs").exists()
    evidence["matrix_reconnect_reconciliation"] = {"read": result, "status": status}


def matrix(report, evidence, base, python, env, children):
    names = ["matrix_bind_and_business", "matrix_volume_unavailable", "matrix_wrong_volume",
             "matrix_disconnect_after_volume_checked", "matrix_disconnect_before_publish",
             "matrix_disconnect_before_reference_commit", "matrix_submitter_disconnect",
             "matrix_daemon_recovery", "matrix_corrupt_payload", "matrix_reconnect_reconciliation"]
    lab_parent = Path(tempfile.mkdtemp(prefix="pointwise-matrix-"))
    lab_area = lab_parent / "lab"
    probe = subprocess.run([python, str(base / "volumes.py"), "open", str(lab_area)],
                           capture_output=True, text=True, timeout=300)
    if probe.returncode != 0:
        reason = (probe.stderr.strip().splitlines() or ["unknown"])[-1]
        for name in names:
            report.record(name, "NOT_EXECUTED", "volume lab unavailable: %s" % reason)
        evidence["volume_lab_unavailable"] = {"returncode": probe.returncode,
                                              "stderr": probe.stderr}
        shutil.rmtree(lab_parent, ignore_errors=True)
        return
    state = json.loads(probe.stdout)
    area = Path(tempfile.mkdtemp(prefix="matrix install "))
    ctx_box = {"lab_area": str(lab_area)}
    try:
        source, package, dest = area / "source", area / "package", area / "安装目录"
        work = area / "empty"
        source.mkdir()
        work.mkdir()
        for path in base.glob("*.py"):
            shutil.copy2(path, source / path.name)
        ok = run_check(report, names[0], matrix_bind, ctx_box, evidence,
                       base, python, env, source, package, dest, work, state, area)
        if ok:
            run_check(report, names[1], matrix_unavailable, ctx_box, evidence, env)
            run_check(report, names[2], matrix_wrong, ctx_box, evidence, env)
            run_check(report, names[3], matrix_disconnect_checked, ctx_box, evidence, env, children)
            run_check(report, names[4], matrix_disconnect_publish, ctx_box, evidence, env, children)
            run_check(report, names[5], matrix_disconnect_commit, ctx_box, evidence, env, children)
            run_check(report, names[6], matrix_submitter, ctx_box, evidence, env, children)
            run_check(report, names[7], matrix_daemon, ctx_box, evidence, env, children)
            run_check(report, names[8], matrix_corrupt, ctx_box, evidence, env)
            run_check(report, names[9], matrix_reconcile, ctx_box, evidence, env)
        else:
            for name in names[1:]:
                report.record(name, "NOT_EXECUTED", "matrix setup failed")
    finally:
        subprocess.run([python, str(base / "volumes.py"), "close", str(lab_area)],
                       capture_output=True, text=True, timeout=120)
        shutil.rmtree(lab_parent, ignore_errors=True)
        shutil.rmtree(area, ignore_errors=True)


def live(report, evidence, base, python, env, children):
    name = "live_launch_agent"
    if sys.platform != "darwin":
        report.record(name, "NOT_EXECUTED",
                      "requires a macOS GUI login session; launchctl is unavailable on %s"
                      % sys.platform)
        return
    probe = subprocess.run(["/bin/launchctl", "print", "gui/%d" % os.getuid()],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        report.record(name, "NOT_EXECUTED",
                      "no GUI login session: %s" % probe.stderr.strip())
        return
    try:
        lab_parent = Path(tempfile.mkdtemp(prefix="pointwise-live-"))
        lab_area = lab_parent / "lab"
        state = call_json([python, str(base / "volumes.py"), "open", str(lab_area)],
                          lab_parent, env, timeout=300)
        area = Path(tempfile.mkdtemp(prefix="live install "))
        work = area / "empty"
        source = area / "source"
        package = area / "package"
        dest = area / "安装目录"
        work.mkdir()
        source.mkdir()
        for path in base.glob("*.py"):
            shutil.copy2(path, source / path.name)
        info = None
        try:
            call_json([python, str(source / "ops.py"), "build", str(package)], work, env)
            info = call_json([python, str(source / "ops.py"), "install", str(package), str(dest),
                              "--python", python, "--payload-dir", state["payload"]], work, env)
            shutil.rmtree(source)
            label = info["label"]
            assert inspect(str(Path.home() / "Library" / "LaunchAgents" / (label + ".plist")),
                           info["app"], info["root"], python) == label
            ctx = Ctx(python, info["app"], info["root"], env, work)
            k1 = make_batch("live-k1")
            ctx.submit(k1, area)
            deadline = time.monotonic() + 120
            while "live-k1" not in [item["batch"] for item in ctx.read()]:
                assert time.monotonic() < deadline, "LaunchAgent worker did not drain live-k1"
                time.sleep(0.5)
            lab_cycle({"base": base, "python": python, "lab_area": str(lab_area)},
                      "offline", str(lab_area))
            status = ctx.status()
            assert status["volume_state"] == "volume_unavailable", status
            refused = ctx.submit(make_batch("live-k2"), area, success=False)
            assert "VOLUME_UNAVAILABLE" in refused.stderr
            lab_cycle({"base": base, "python": python, "lab_area": str(lab_area)},
                      "mount", str(lab_area), "foreign")
            status = ctx.status()
            assert status["volume_state"] == "wrong_volume", status
            lab_cycle({"base": base, "python": python, "lab_area": str(lab_area)},
                      "offline", str(lab_area))
            lab_cycle({"base": base, "python": python, "lab_area": str(lab_area)},
                      "mount", str(lab_area), "original")
            k2 = make_batch("live-k2")
            ctx.submit(k2, area)
            deadline = time.monotonic() + 120
            while "live-k2" not in [item["batch"] for item in ctx.read()]:
                assert time.monotonic() < deadline, "LaunchAgent worker did not recover"
                time.sleep(0.5)
            result = ctx.read()
            assert_batch(result, k1)
            assert_batch(result, k2)
            printed = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                                     capture_output=True, text=True, timeout=30)
            evidence["live_launch_agent"] = {"read": result, "label": label,
                                             "launchctl": printed.stdout}
        finally:
            if info is not None:
                subprocess.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (os.getuid(), info["label"])],
                               capture_output=True, timeout=30)
                plist = Path.home() / "Library" / "LaunchAgents" / (info["label"] + ".plist")
                plist.unlink(missing_ok=True)
            subprocess.run([python, str(base / "volumes.py"), "close", str(lab_area)],
                           capture_output=True, text=True, timeout=120)
            shutil.rmtree(lab_parent, ignore_errors=True)
            shutil.rmtree(area, ignore_errors=True)
    except Exception:
        report.record(name, "FAIL", traceback.format_exc())
        return
    report.record(name, "PASS")


def verify(demo=False):
    base = Path(__file__).resolve().parent
    python = str(Path(sys.executable).resolve())
    env = dict(os.environ, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    for key in ("PYTHONPATH", "PYTHONHOME", "POINTWISE_PAUSE", "POINTWISE_GATE"):
        env.pop(key, None)
    report = Report()
    evidence = {"platform": sys.platform, "python": python}
    children = []
    try:
        run_check(report, "installed_baseline", baseline, evidence, base, python, env, children)
        external(report, evidence, base, python, env, children)
        matrix(report, evidence, base, python, env, children)
        live(report, evidence, base, python, env, children)
    finally:
        for child in children:
            try:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=30)
            except Exception:
                pass
    failed = [item for item in report.items if item["status"] == "FAIL"]
    skipped = [item for item in report.items if item["status"] == "NOT_EXECUTED"]
    passed = [item for item in report.items if item["status"] == "PASS"]
    summary = {"passed": len(passed), "failed": len(failed), "not_executed": len(skipped),
               "not_executed_names": [item["name"] for item in skipped]}
    if demo:
        print(json.dumps({"summary": summary, "checks": report.items, "evidence": evidence},
                         ensure_ascii=False, sort_keys=True, indent=2))
    else:
        for item in report.items:
            print("%s: %s" % (item["status"], item["name"]))
        for item in failed:
            print("---- FAILED: %s ----\n%s" % (item["name"], item["detail"]))
        if failed:
            print("RELEASECHECK_FAILED: %d failed, %d passed, %d not executed"
                  % (len(failed), len(passed), len(skipped)))
        else:
            print("RELEASECHECK_OK: %d passed, %d not executed" % (len(passed), len(skipped)))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    verify(demo=len(sys.argv) > 1 and sys.argv[1] == "demo")
