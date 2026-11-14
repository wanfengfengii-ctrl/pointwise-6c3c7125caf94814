"""macOS/Linux mount-cycle lab; no production volume guard or recovery implementation.

On Darwin the lab creates real sparse disk images and attaches them with
hdiutil. On Linux it bind-mounts per-volume image directories with the system
mount/umount tools, which requires mount privileges (root or CAP_SYS_ADMIN);
where the kernel refuses, commands fail and the caller must record the volume
experiments as not executed instead of simulating them.
"""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys

DARWIN = sys.platform == "darwin"


def invoke(argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def load(area):
    area = Path(area).resolve()
    return area, json.loads((area / "volumes.json").read_text(encoding="utf-8"))


def save(area, state):
    (area / "volumes.json").write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def attach(image, mount):
    mount.mkdir(parents=True, exist_ok=True)
    if any(mount.iterdir()):
        raise ValueError("MOUNTPOINT_NOT_EMPTY")
    data = invoke(["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), "-plist", str(image)])
    entries = plistlib.loads(data)["system-entities"]
    entry = next(x for x in entries if x.get("mount-point") == str(mount))
    return entry["dev-entry"]


def detach(state, force=False):
    if state["device"] is None:
        return
    argv = ["/usr/bin/hdiutil", "detach"]
    if force:
        argv.append("-force")
    invoke(argv + [state["device"]])
    state["device"] = None
    state["mounted"] = None


def linux_attach(image, mount):
    mount.mkdir(parents=True, exist_ok=True)
    if any(mount.iterdir()):
        raise ValueError("MOUNTPOINT_NOT_EMPTY")
    invoke(["mount", "--bind", str(image), str(mount)])
    return str(image)


def linux_detach(state, force=False):
    if state["device"] is None:
        return
    argv = ["umount"]
    if force:
        # Lazy detach tolerates descriptors pinned by open workers, matching
        # hdiutil detach -force semantics for the disconnect experiments.
        argv.append("-l")
    invoke(argv + [state["mount"]])
    state["device"] = None
    state["mounted"] = None


def open_lab(area):
    return open_lab_darwin(area) if DARWIN else open_lab_linux(area)


def open_lab_darwin(area):
    area = Path(area).resolve()
    area.mkdir(parents=True, exist_ok=False)
    mount = area / "载荷挂载点"
    mount.mkdir()
    state = {"mount": str(mount), "payload": str(mount / "payload"),
             "device": None, "mounted": None, "images": {}, "uuids": {}}
    try:
        for name in ("original", "foreign"):
            image = area / (name + ".sparseimage")
            invoke(["/usr/bin/hdiutil", "create", "-size", "64m", "-fs", "HFS+J",
                    "-type", "SPARSE", "-volname", "pointwise-" + name, str(image)])
            state["images"][name] = str(image)
            state["device"] = attach(image, mount)
            state["mounted"] = name
            (mount / "payload").mkdir()
            info = plistlib.loads(invoke(["/usr/sbin/diskutil", "info", "-plist", str(mount)]))
            state["uuids"][name] = info["VolumeUUID"]
            detach(state)
        assert state["uuids"]["original"] != state["uuids"]["foreign"]
        state["device"] = attach(Path(state["images"]["original"]), mount)
        state["mounted"] = "original"
        save(area, state)
        return state
    except BaseException:
        detach(state, force=True)
        raise


def open_lab_linux(area):
    area = Path(area).resolve()
    area.mkdir(parents=True, exist_ok=False)
    mount = area / "载荷挂载点"
    mount.mkdir()
    state = {"mount": str(mount), "payload": str(mount / "payload"),
             "device": None, "mounted": None, "images": {}, "uuids": {},
             "backend": "linux-bind"}
    try:
        for name in ("original", "foreign"):
            image = area / "images" / name
            (image / "payload").mkdir(parents=True)
            state["images"][name] = str(image)
            state["device"] = linux_attach(image, mount)
            state["mounted"] = name
            from volguard import mounted_identity
            state["uuids"][name] = mounted_identity(str(mount))
            linux_detach(state)
        assert state["uuids"]["original"] != state["uuids"]["foreign"]
        state["device"] = linux_attach(Path(state["images"]["original"]), mount)
        state["mounted"] = "original"
        save(area, state)
        return state
    except BaseException:
        try:
            linux_detach(state, force=True)
        except Exception:
            pass
        raise


def cycle(command, area, which=None):
    area, state = load(area)
    linux = state.get("backend") == "linux-bind"
    do_attach = linux_attach if linux else attach
    do_detach = linux_detach if linux else detach
    if command == "mount":
        if state["mounted"] is not None:
            raise ValueError("DETACH_FIRST")
        state["device"] = do_attach(Path(state["images"][which]), Path(state["mount"]))
        state["mounted"] = which
    else:
        do_detach(state, force=command == "offline")
    save(area, state)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["open", "offline", "mount", "close"])
    parser.add_argument("area")
    parser.add_argument("which", nargs="?", choices=["original", "foreign"])
    args = parser.parse_args()
    if args.command == "mount" and args.which is None:
        parser.error("mount requires original or foreign")
    result = open_lab(args.area) if args.command == "open" else cycle(args.command, args.area, args.which)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
