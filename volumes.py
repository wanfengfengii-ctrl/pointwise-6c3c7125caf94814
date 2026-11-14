"""macOS mount-cycle lab; no production volume guard or recovery implementation."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess


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


def open_lab(area):
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


def cycle(command, area, which=None):
    area, state = load(area)
    if command == "mount":
        if state["mounted"] is not None:
            raise ValueError("DETACH_FIRST")
        state["device"] = attach(Path(state["images"][which]), Path(state["mount"]))
        state["mounted"] = which
    else:
        detach(state, force=command == "offline")
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
