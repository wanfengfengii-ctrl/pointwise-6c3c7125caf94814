"""External payload volume binding, identity guard, and guarded blob store.

An installation enters external volume mode when ops.py install records a
binding in ROOT/volume.json. The binding captures the identity of the volume
mounted at the agreed location together with the payload directory inside it.
Every payload read or write re-verifies that identity against the live mount
table and then pins the verified payload directory with a file descriptor, so
a disconnect or a foreign volume occupying the mountpoint after the check can
never receive payload bytes, temporary files, publishes, or cleanup.

Availability problems raise VolumeError (retryable once the original volume
is reattached at the agreed location). Missing or corrupt payloads on a
healthy bound volume are data faults (PAYLOAD_MISSING, PAYLOAD_HASH,
PAYLOAD_JSON) and never masquerade as volume problems.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

from pipeline import encoded


class VolumeError(ValueError):
    """Base class for retryable payload volume availability errors."""


class VolumeUnavailable(VolumeError):
    def __init__(self, detail=""):
        super().__init__("VOLUME_UNAVAILABLE" + (": " + detail if detail else ""))


class WrongVolume(VolumeError):
    def __init__(self, detail=""):
        super().__init__("WRONG_VOLUME" + (": " + detail if detail else ""))


DARWIN = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")


def _unescape_mount(value):
    # /proc/self/mounts escapes space, tab, newline and backslash as octal.
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 3 < len(value) and value[i + 1:i + 4].isdigit():
            out.append(chr(int(value[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _linux_mounts():
    entries = []
    with open("/proc/self/mounts", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            if len(fields) >= 3:
                entries.append((_unescape_mount(fields[1]), fields[0], fields[2]))
    return entries


def _linux_mount_entry(mountpoint):
    for path, source, fstype in _linux_mounts():
        if path == mountpoint:
            return source, fstype
    return None


def _linux_mountpoint(path):
    best = None
    for mountpoint, _source, _fstype in _linux_mounts():
        if mountpoint != "/" and not (
                path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")):
            continue
        if best is None or len(mountpoint) > len(best):
            best = mountpoint
    return best


def _darwin_mountpoint(path):
    current = path
    while not os.path.ismount(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current


def _by_uuid(source):
    # Resolve a block device to its filesystem UUID via /dev/disk/by-uuid
    # symlinks; no subprocess and no root required. Bind mounts report the
    # hosting filesystem here, so the mount root inode stays part of the
    # identity and keeps same-filesystem volumes distinguishable.
    if not source.startswith("/dev/"):
        return ""
    directory = Path("/dev/disk/by-uuid")
    if not directory.is_dir():
        return ""
    try:
        wanted = os.path.realpath(source)
        for entry in directory.iterdir():
            if os.path.realpath(str(entry)) == wanted:
                return entry.name
    except OSError:
        pass
    return ""


def _linux_identity(mountpoint, source):
    st = os.stat(mountpoint)
    uuid = _by_uuid(source)
    if uuid:
        return "U%s:I%d" % (uuid, st.st_ino)
    return "D%d:I%d" % (st.st_dev, st.st_ino)


def _darwin_identity(mountpoint):
    result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", mountpoint],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise VolumeUnavailable("diskutil cannot read %s: %s" % (
            mountpoint, result.stderr.decode("utf-8", "replace").strip()))
    uuid = plistlib.loads(result.stdout).get("VolumeUUID")
    if not uuid:
        raise VolumeUnavailable("no VolumeUUID at %s" % mountpoint)
    return "U" + uuid


def mountpoint_of(path):
    """The mountpoint covering path, or None when the platform is unsupported."""
    if DARWIN:
        return _darwin_mountpoint(path)
    if LINUX:
        return _linux_mountpoint(path)
    raise ValueError("PLATFORM_UNSUPPORTED: %s" % sys.platform)


def mounted_identity(mountpoint):
    """Identity of the volume mounted exactly at mountpoint; None if none."""
    if DARWIN:
        if not os.path.ismount(mountpoint):
            return None
        return _darwin_identity(mountpoint)
    if LINUX:
        entry = _linux_mount_entry(mountpoint)
        if entry is None:
            return None
        return _linux_identity(mountpoint, entry[0])
    raise ValueError("PLATFORM_UNSUPPORTED: %s" % sys.platform)


def bind(payload_dir):
    """Record the identity of the volume holding an existing payload dir."""
    directory = Path(payload_dir).resolve()
    if not directory.is_dir():
        raise ValueError("PAYLOAD_DIR_MISSING: %s" % payload_dir)
    if not os.access(str(directory), os.W_OK | os.X_OK):
        raise ValueError("PAYLOAD_DIR_NOT_WRITABLE: %s" % directory)
    mountpoint = mountpoint_of(str(directory))
    if not mountpoint or mountpoint == "/":
        raise ValueError("PAYLOAD_DIR_NOT_ON_VOLUME: %s" % directory)
    identity = mounted_identity(mountpoint)
    if identity is None:
        raise ValueError("PAYLOAD_DIR_NOT_ON_VOLUME: %s" % directory)
    return {"version": 1, "payload_dir": str(directory),
            "mountpoint": mountpoint, "identity": identity}


def load_binding(root):
    path = Path(root) / "volume.json"
    if not path.exists():
        return None
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("BINDING_CORRUPT: %s" % exc)
    if (not isinstance(binding, dict) or binding.get("version") != 1 or
            not all(isinstance(binding.get(key), str) and binding[key]
                    for key in ("payload_dir", "mountpoint", "identity"))):
        raise ValueError("BINDING_CORRUPT: %s" % path)
    return binding


class VolumeGuard:
    """Re-verifies the bound volume before each payload operation."""

    def __init__(self, binding, hook=None):
        self.payload_dir = binding["payload_dir"]
        self.mountpoint = binding["mountpoint"]
        self.identity = binding["identity"]
        self.hook = hook or (lambda event, detail: None)

    def require(self):
        """Verify the bound volume; return the payload dir (dev, ino) pair."""
        mountpoint = self.mountpoint
        if not os.path.isdir(mountpoint):
            raise VolumeUnavailable("mountpoint missing: %s" % mountpoint)
        try:
            current = mounted_identity(mountpoint)
        except VolumeUnavailable:
            raise
        except OSError as exc:
            raise VolumeUnavailable("identity query failed: %s" % exc) from exc
        if current is None:
            raise VolumeUnavailable("no volume mounted at %s" % mountpoint)
        if current != self.identity:
            raise WrongVolume("foreign volume occupies %s" % mountpoint)
        try:
            st = os.stat(self.payload_dir)
        except OSError as exc:
            raise VolumeUnavailable("payload directory unavailable: %s" % exc) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise VolumeUnavailable("payload path is not a directory: %s" % self.payload_dir)
        return st.st_dev, st.st_ino

    def probe(self):
        try:
            self.require()
        except VolumeUnavailable:
            return "volume_unavailable"
        except WrongVolume:
            return "wrong_volume"
        return "ready"

    def checked_fd(self, operation):
        """Verify identity and pin the payload directory with a descriptor.

        The volume_checked observation point fires after verification while
        the descriptor is held, before the payload file itself is touched;
        every subsequent access in this operation goes through the pinned
        descriptor and can never be redirected to a bare mountpoint or a
        foreign volume.
        """
        attempts = 0
        while True:
            expected = self.require()
            try:
                fd = os.open(self.payload_dir,
                             os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError as exc:
                raise VolumeUnavailable("payload directory open failed: %s" % exc) from exc
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == expected:
                break
            os.close(fd)
            attempts += 1
            if attempts >= 3:
                raise VolumeUnavailable("volume changed during identity check")
        self.hook("volume_checked", {"operation": operation})
        return fd


class ExternalBlobs:
    """Payload store on the bound external volume; never creates directories."""

    def __init__(self, binding, hook=None):
        self.hook = hook or (lambda event, detail: None)
        self.guard = VolumeGuard(binding, self.hook)
        self.directory = binding["payload_dir"]

    def state(self):
        return self.guard.probe()

    def require_ready(self):
        self.guard.require()

    def _translate(self, exc):
        # An I/O error right after a successful identity check means the
        # volume was lost in between; reclassify it from the live mount
        # table instead of reporting a plain I/O error.
        state = self.guard.probe()
        if state == "wrong_volume":
            return WrongVolume("bound volume lost during payload I/O")
        if state == "volume_unavailable":
            return VolumeUnavailable("bound volume lost during payload I/O")
        return exc

    def put(self, value):
        data = encoded(value)
        oid = hashlib.sha256(data).hexdigest()
        fd = self.guard.checked_fd("put")
        temporary = None
        try:
            for _ in range(8):
                candidate = ".writing-%d-%s" % (os.getpid(), os.urandom(6).hex())
                try:
                    stream = os.open(candidate,
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     0o600, dir_fd=fd)
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if temporary is None:
                raise OSError("temporary name collision in payload directory")
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(stream, view):]
                os.fsync(stream)
            finally:
                os.close(stream)
            self.hook("before_object_publish", {"oid": oid, "temporary": temporary})
            os.rename(temporary, oid + ".json", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            temporary = None
        except OSError as exc:
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except OSError:
                    pass
            os.close(fd)
        return oid

    def get(self, oid):
        name = oid + ".json"
        fd = self.guard.checked_fd("get")
        try:
            try:
                stream = os.open(name, os.O_RDONLY, dir_fd=fd)
            except FileNotFoundError:
                raise ValueError("PAYLOAD_MISSING")
            try:
                chunks = []
                while True:
                    chunk = os.read(stream, 1 << 20)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            finally:
                os.close(stream)
        except OSError as exc:
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != oid:
            raise ValueError("PAYLOAD_HASH")
        try:
            return json.loads(data)
        except ValueError as exc:
            raise ValueError("PAYLOAD_JSON") from exc

    def inventory(self):
        try:
            self.guard.require()
            fd = os.open(self.directory,
                         os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except (VolumeError, OSError):
            return {}
        try:
            result = {}
            for name in os.listdir(fd):
                if not name.endswith(".json"):
                    continue
                try:
                    result[name[:-5]] = os.stat(name, dir_fd=fd).st_size
                except OSError:
                    pass
            return result
        except OSError:
            return {}
        finally:
            os.close(fd)
