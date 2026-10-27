"""
The rig a guest boots: its PCIe topology and devices
====================================================

A rig is a cijoe config with a '[rig]' table, used by the rig_* scripts, which
start a qemu guest with it, plug and unplug its devices, and kill it:

    [rig.pcie.switches.p2p]
    downstream_ports = 2

    [rig.devices.nvme0]
    provider = "qemu"
    port = "p2p_dn0"
    device = "nvme,serial=deadbeef"
    drives = ["file=/path/nvme0.img,if=none,id=nvm0,format=raw"]
    children = ["nvme-ns,drive=nvm0,nsid=1"]
    image = {path = "/path/nvme0.img", size = "1G"}

    [rig.devices.gpu0]
    provider = "vfio-user"
    port = "p2p_dn1"
    boot = false
    socket = "/tmp/gpu0.sock"
    server = "/usr/bin/some-server --socket {socket}"

Devices are tables named for the device, in the order the file lists them. A
config layered on the rig, as cijoe merges tables, changes a field of a device
by naming it, '[rig.devices.nic0]' with only that field, and a device it adds
comes after the rig's own, so theirs keep their ports and addresses. Fields
the scripts do not know are left for the rig's users, such as a NIC's
'transport', how the guest reaches the target at its far end.

Each switch sits on a root port of its own, and its downstream ports are named
'<switch>_dn<i>'. A device without a 'port' gets a root port of its own, named
'<device>_rp'. Ports are created in the order the switches, then the devices,
are listed, so a guest's PCIe addresses stay the same from boot to boot.

A device of provider 'qemu' is one QEMU emulates: 'device' is its '-device'
argument without 'id' and 'bus', which are added, 'drives' are '-drive'
arguments, 'children' are '-device' arguments put on the bus the device
provides, such as an NVMe controller's namespaces, and 'image', when given, is
created as a sparse file when it is missing. A device that sits on no bus, such
as an 'nvme-subsys' its controllers name, sets 'bus = false', and gets neither
a port nor children.

A device of provider 'vfio-user' is served by a process on the initiator:
'server' is its command line, with '{socket}' replaced by 'socket' and
'{pid:<device>}' by the pid of that device's server, started before it. Its
output goes to 'socket' with the suffix '.log'. 'driver' defaults to
'vfio-user-pci', and 'props' is a table of further properties, typed as QEMU
wants them, as they are passed as JSON; 'rombar' defaults to 0. A server runs
for as long as the guest does, from before the guest starts to after it is
killed, whether or not its device is plugged in, since one server may need
another's memory, as the NIC's does the GPU's VRAM.

A device with 'boot = false' is not present at boot: its port and drives are,
and 'plug()' adds it over QMP, as 'unplug()' removes it. Its children are
plugged after it, onto its bus, which works only where that bus takes
hot-plugged devices; an NVMe controller's does not, so a controller plugged
later takes its drive itself, 'nvme,drive=...', rather than 'nvme-ns' children.
"""

import errno
import json
import logging as log
import os
import re
import shlex
import signal
import socket as sockets
import time
from pathlib import Path

PROVIDERS = ("qemu", "vfio-user")


class Topology:
    """The ports and devices a rig describes"""

    @classmethod
    def from_config(cls, cijoe):
        """The config's '[rig]', read past getconf(), which takes $RIG over it"""

        return cls(cijoe.config.options.get("rig", {}))

    def __init__(self, rig: dict):
        self.switches = rig.get("pcie", {}).get("switches", {})
        devices = rig.get("devices", {})
        if not isinstance(devices, dict):
            raise ValueError("devices are tables by name, '[rig.devices.<name>]'")
        self.devices = [{**dev, "name": name} for name, dev in devices.items()]

        self.ports = {}
        for switch, cfg in self.switches.items():
            for i in range(int(cfg.get("downstream_ports", 1))):
                self.ports[f"{switch}_dn{i}"] = switch

        for dev in self.devices:
            if dev.get("provider") not in PROVIDERS:
                raise ValueError(f"device {dev['name']}: provider not in {PROVIDERS}")
            port = dev.get("port")
            if port is not None and port not in self.ports:
                raise ValueError(f"device {dev['name']}: no port named {port}")
            if dev.get("bus", True) is False and (
                port or dev.get("children") or dev["provider"] != "qemu"
            ):
                raise ValueError(
                    f"device {dev['name']}: 'bus = false' is a qemu device without"
                    " a port or children"
                )

        taken = [dev["port"] for dev in self.devices if dev.get("port")]
        if len(set(taken)) != len(taken):
            raise ValueError(f"two devices on one port; ports {taken}")

    def device(self, name: str):
        """The device named 'name'; raises KeyError when there is none"""

        for dev in self.devices:
            if dev["name"] == name:
                return dev

        raise KeyError(name)

    def served(self):
        """The devices a server serves, in the order they are listed"""

        return [dev for dev in self.devices if dev["provider"] == "vfio-user"]

    @staticmethod
    def bus(dev: dict):
        """The bus 'dev' goes on; None when it goes on none"""

        if dev.get("bus", True) is False:
            return None

        return dev.get("port") or f"{dev['name']}_rp"

    def port_args(self):
        """The '-device' arguments creating the root ports and switches"""

        args = []
        chassis = 0

        def root_port(name):
            nonlocal chassis
            chassis += 1
            return ["-device", f"pcie-root-port,id={name},chassis={chassis},slot=0"]

        for switch, cfg in self.switches.items():
            args += root_port(f"{switch}_rp")
            args += ["-device", f"x3130-upstream,id={switch}_up,bus={switch}_rp"]
            for i in range(int(cfg.get("downstream_ports", 1))):
                chassis += 1
                args += [
                    "-device",
                    f"xio3130-downstream,id={switch}_dn{i},bus={switch}_up"
                    f",chassis={chassis},slot={i}",
                ]

        for dev in self.devices:
            if not dev.get("port") and self.bus(dev):
                args += root_port(self.bus(dev))

        return args

    def drive_args(self):
        """The '-drive' arguments of every device, present or not, shell-quoted"""

        args = []
        for dev in self.devices:
            for drive in dev.get("drives", []):
                args += ["-drive", shlex.quote(drive)]

        return args

    def qemu_device_specs(self, dev: dict):
        """The '-device' arguments, as strings, of a 'qemu' device and its children"""

        bus = self.bus(dev)
        specs = [f"{dev['device']},id={dev['name']}" + (f",bus={bus}" if bus else "")]
        for i, child in enumerate(dev.get("children", [])):
            specs.append(f"{child},id={dev['name']}_c{i},bus={dev['name']}")

        return specs

    def vfio_user_props(self, dev: dict):
        """The QMP/JSON properties of a 'vfio-user' device"""

        props = {"rombar": 0, **dev.get("props", {})}
        props.update(
            {
                "driver": dev.get("driver", "vfio-user-pci"),
                "id": dev["name"],
                "bus": self.bus(dev),
                "socket": {"type": "unix", "path": dev["socket"]},
            }
        )

        return props

    def boot_device_args(self):
        """The '-device' arguments of the devices present at boot, shell-quoted, as
        qemu is run through a shell"""

        args = []
        for dev in self.devices:
            if not dev.get("boot", True):
                continue
            if dev["provider"] == "qemu":
                for spec in self.qemu_device_specs(dev):
                    args += ["-device", shlex.quote(spec)]
            else:
                args += ["-device", shlex.quote(json.dumps(self.vfio_user_props(dev)))]

        return args


def memory_args(guest_config: dict):
    """A shared memory backend, which a vfio-user server needs to map guest memory;
    sized as the guest's '-m', which qemu requires it to match"""

    mem = guest_config.get("system_args", {}).get("kwa", {}).get("m", "128M")

    return [
        "-object",
        f"memory-backend-memfd,id=rig_mem,size={mem},share=on",
        "-machine",
        "memory-backend=rig_mem",
    ]


class Server:
    """The process serving a 'vfio-user' device, its pid kept in the guest's home"""

    def __init__(self, cijoe, guest_path: Path, dev: dict):
        self.cijoe = cijoe
        self.guest_path = guest_path
        self.dev = dev
        self.socket = Path(dev["socket"])
        self.pidfile = guest_path / f"{dev['name']}.server.pid"
        self.log = self.socket.with_suffix(".log")

    def pid(self):
        try:
            pid = int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return 0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            pass

        return pid

    def command(self):
        """The server's command line, its placeholders replaced; None when a
        '{pid:<device>}' names a device whose server is not running"""

        def pid_of(match):
            other = {**self.dev, "name": match.group(1), "socket": "/dev/null"}
            pid = Server(self.cijoe, self.guest_path, other).pid()
            if not pid:
                raise LookupError(match.group(1))
            return str(pid)

        command = self.dev["server"].replace("{socket}", str(self.socket))
        try:
            return re.sub(r"\{pid:([^}]+)\}", pid_of, command)
        except LookupError as exc:
            log.error(f"{self.dev['name']}: the server of {exc} is not running")
            return None

    def stop(self, timeout=10):
        """Stop the server; returns 0, as a server not running is stopped"""

        pid = self.pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            began = time.time()
            while self.pid() and time.time() - began < timeout:
                time.sleep(0.1)
            if self.pid():
                os.kill(pid, signal.SIGKILL)
        self.pidfile.unlink(missing_ok=True)

        return 0

    def start(self, timeout=30):
        """Start the server, replacing one left running, and wait for its socket

        A vfio-user session does not survive its server, so a stale one is
        stopped rather than reused.
        """

        self.stop()
        self.socket.unlink(missing_ok=True)
        self.socket.parent.mkdir(parents=True, exist_ok=True)

        command = self.command()
        if command is None:
            return errno.ESRCH

        err, _ = self.cijoe.run_local(
            f"setsid {command} > {self.log} 2>&1 < /dev/null & echo $! > {self.pidfile}"
        )
        if err:
            log.error(f"{self.dev['name']}: could not start server; err({err})")
            return err

        began = time.time()
        while not self.socket.is_socket():
            if not self.pid():
                log.error(f"{self.dev['name']}: server exited; see {self.log}")
                return errno.EIO
            if time.time() - began > timeout:
                log.error(
                    f"{self.dev['name']}: no socket {self.socket}; see {self.log}"
                )
                return errno.ETIMEDOUT
            time.sleep(0.1)

        log.info(f"{self.dev['name']}: server listening on {self.socket}")
        return 0


class QMP:
    """A minimal client for the guest's QMP socket"""

    def __init__(self, path: Path, timeout=30):
        self.sock = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rw")
        self.events: list = []
        self._read()  # the greeting
        self.execute("qmp_capabilities")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()
        self.sock.close()

    def _read(self):
        line = self.stream.readline()
        if not line:
            raise ConnectionError("QMP socket closed")
        return json.loads(line)

    def execute(self, command: str, arguments=None):
        """Run 'command'; returns its reply, keeping the events read meanwhile"""

        msg = {"execute": command}
        if arguments is not None:
            msg["arguments"] = arguments
        self.stream.write(json.dumps(msg) + "\n")
        self.stream.flush()

        while True:
            reply = self._read()
            if "event" in reply:
                self.events.append(reply)
                continue
            return reply

    def wait_event(self, name: str, device: str, timeout: float):
        """Wait for event 'name' about 'device'; True when it came"""

        def match(ev):
            return ev["event"] == name and ev.get("data", {}).get("device") == device

        if any(match(ev) for ev in self.events):
            return True

        began = time.time()
        while time.time() - began < timeout:
            self.sock.settimeout(max(0.1, timeout - (time.time() - began)))
            try:
                ev = self._read()
            except (TimeoutError, sockets.timeout):
                break
            if "event" in ev:
                self.events.append(ev)
                if match(ev):
                    return True

        return False


def qmp_path(guest_path: Path):
    return guest_path / "qmp.sock"


def guest_args(topo: Topology, guest_config: dict, guest_path: Path):
    """The qemu arguments of the rig, for Guest.start(extra_args=...)"""

    args = []
    if topo.served():
        args += memory_args(guest_config)
    args += topo.port_args()
    args += topo.drive_args()
    args += topo.boot_device_args()
    args += ["-qmp", f"unix:{qmp_path(guest_path)},server,nowait"]

    return args


def create_images(cijoe, topo: Topology):
    """Create the missing images the devices name"""

    for dev in topo.devices:
        image = dev.get("image")
        if not image or Path(image["path"]).exists():
            continue
        Path(image["path"]).parent.mkdir(parents=True, exist_ok=True)
        err, _ = cijoe.run_local(f"truncate -s {image['size']} {image['path']}")
        if err:
            return err

    return 0


def start_servers(cijoe, topo: Topology, guest_path: Path):
    """Start every device's server, in the order the devices are listed"""

    for dev in topo.served():
        err = Server(cijoe, guest_path, dev).start()
        if err:
            return err

    return 0


def stop_servers(cijoe, topo: Topology, guest_path: Path):
    """Stop every device's server, the last started first"""

    for dev in reversed(topo.served()):
        Server(cijoe, guest_path, dev).stop()


def plug(guest_path: Path, topo: Topology, name: str):
    """Add the device 'name' to the running guest"""

    dev = topo.device(name)

    with QMP(qmp_path(guest_path)) as qmp:
        if dev["provider"] == "qemu":
            # As on the command line; QMP's own device_add wants typed values
            for i, spec in enumerate(topo.qemu_device_specs(dev)):
                reply = qmp.execute(
                    "human-monitor-command", {"command-line": f"device_add {spec}"}
                )
                if "error" in reply or reply.get("return"):
                    log.error(f"device_add {spec}: {reply}")
                    if i:  # the device went in, a child did not; take it out
                        qmp.execute("device_del", {"id": name})
                    return errno.EIO
        else:
            reply = qmp.execute("device_add", topo.vfio_user_props(dev))
            if "error" in reply:
                log.error(f"device_add {name}: {reply['error']}")
                return errno.EIO

    return 0


def unplug(guest_path: Path, topo: Topology, name: str, timeout=60):
    """Remove the device 'name' from the running guest; waits for QEMU to report
    it deleted, at most 'timeout' seconds, as the guest has to release it"""

    topo.device(name)

    with QMP(qmp_path(guest_path)) as qmp:
        reply = qmp.execute("device_del", {"id": name})
        if "error" in reply:
            log.error(f"device_del {name}: {reply['error']}")
            return errno.EIO
        if not qmp.wait_event("DEVICE_DELETED", name, timeout):
            log.error(f"{name} was not deleted within {timeout} seconds")
            return errno.ETIMEDOUT

    return 0
