"""Unit tests for rig.py, the PCIe topology and devices a guest boots with.

The argument tests check what the rig gives qemu, so they need no qemu. The
server and QMP tests run a stand-in server and a stand-in QMP socket, so they
need none either. Real rigs are booted by the qualify workflows.
"""

import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import rig
from cijoe.core.command import Cijoe
from cijoe.core.resources import Config
from rig import QMP, Server


def keyed(rig_dict):
    """The rig with its devices by name, from devices listed with their names, as
    the tests write them"""

    devices = rig_dict.get("devices")
    if not isinstance(devices, list):
        return rig_dict
    by_name = {d["name"]: {k: v for k, v in d.items() if k != "name"} for d in devices}
    return {**rig_dict, "devices": by_name}


def Topology(rig_dict):
    return rig.Topology(keyed(rig_dict))


SWITCHED = {
    "pcie": {"switches": {"sw": {"downstream_ports": 2}}},
    "devices": [
        {
            "name": "nvme0",
            "provider": "qemu",
            "port": "sw_dn0",
            "device": "nvme,serial=deadbeef",
            "drives": ["file=nvme0.img,if=none,id=nvm0,format=raw"],
            "children": ["nvme-ns,drive=nvm0,nsid=1"],
        },
        {
            "name": "gpu0",
            "provider": "vfio-user",
            "port": "sw_dn1",
            "boot": False,
            "socket": "/tmp/gpu0.sock",
            "server": "server --socket {socket}",
            "props": {"x-msg-timeout": 1000},
        },
        {
            "name": "nvme1",
            "provider": "qemu",
            "device": "nvme,serial=cafe",
            "boot": False,
        },
    ],
}


def test_empty_rig_gives_only_qmp():
    assert rig.guest_args(Topology({}), {}, Path("/g")) == [
        "-qmp",
        "unix:/g/qmp.sock,server,nowait",
    ]


def test_ports_in_order():
    assert Topology(SWITCHED).port_args() == [
        "-device",
        "pcie-root-port,id=sw_rp,chassis=1,slot=0",
        "-device",
        "x3130-upstream,id=sw_up,bus=sw_rp",
        "-device",
        "xio3130-downstream,id=sw_dn0,bus=sw_up,chassis=2,slot=0",
        "-device",
        "xio3130-downstream,id=sw_dn1,bus=sw_up,chassis=3,slot=1",
        "-device",
        "pcie-root-port,id=nvme1_rp,chassis=4,slot=0",
    ]


def test_drives_present_whether_booted_or_not():
    assert Topology(SWITCHED).drive_args() == [
        "-drive",
        "file=nvme0.img,if=none,id=nvm0,format=raw",
    ]


def test_only_boot_devices_at_boot():
    assert Topology(SWITCHED).boot_device_args() == [
        "-device",
        "nvme,serial=deadbeef,id=nvme0,bus=sw_dn0",
        "-device",
        "nvme-ns,drive=nvm0,nsid=1,id=nvme0_c0,bus=nvme0",
    ]


def test_vfio_user_props_typed():
    topo = Topology(SWITCHED)
    assert topo.json_props(topo.device("gpu0")) == {
        "rombar": 0,
        "x-msg-timeout": 1000,
        "driver": "vfio-user-pci",
        "id": "gpu0",
        "bus": "sw_dn1",
        "socket": {"type": "unix", "path": "/tmp/gpu0.sock"},
    }


def test_vfio_user_at_boot_is_json():
    topo = Topology(
        {
            "devices": [
                {"name": "d", "provider": "vfio-user", "socket": "/s", "server": "x"}
            ]
        }
    )
    args = topo.boot_device_args()
    assert args[0] == "-device"
    assert args[1].startswith("'") and args[1].endswith("'")
    assert json.loads(args[1][1:-1])["bus"] == "d_rp"
    assert topo.served() == topo.devices


@pytest.mark.parametrize(
    "guest_config",
    [
        {"devices": [{"name": "a", "provider": "nope"}]},
        {"devices": [{"name": "a", "provider": "qemu", "port": "nowhere"}]},
        {
            "pcie": {"switches": {"sw": {}}},
            "devices": [
                {"name": "a", "provider": "qemu", "port": "sw_dn0"},
                {"name": "b", "provider": "qemu", "port": "sw_dn0"},
            ],
        },
    ],
    ids=["unknown-provider", "unknown-port", "port-twice"],
)
def test_invalid(guest_config):
    with pytest.raises(ValueError):
        Topology(guest_config)


def make_cijoe(tmp_path):
    config = Config(Path("guest.toml"))
    config.options = {}
    cijoe = Cijoe(config, tmp_path / "output", False)
    cijoe.set_output_ident("test")
    return cijoe


# Creates the socket it is given, then waits to be stopped
FAKE_SERVER = """
import socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1])
s.listen()
time.sleep(600)
"""


def fake_server_dev(tmp_path, script=FAKE_SERVER):
    path = tmp_path / "server.py"
    path.write_text(script)
    return {
        "name": "d",
        "provider": "vfio-user",
        "socket": str(tmp_path / "d.sock"),
        "server": f"{sys.executable} {path} {{socket}}",
    }


def test_server_start_and_stop(tmp_path):
    server = Server(make_cijoe(tmp_path), tmp_path, fake_server_dev(tmp_path))

    assert server.start() == 0
    pid = server.pid()
    assert pid
    assert server.socket.is_socket()

    assert server.start() == 0, "a second start replaces the first server"
    assert server.pid() != pid

    assert server.stop() == 0
    assert not server.pid()
    assert not server.pidfile.exists()


def test_server_that_exits(tmp_path):
    dev = fake_server_dev(tmp_path, "import sys; sys.exit(1)")
    server = Server(make_cijoe(tmp_path), tmp_path, dev)

    assert server.start() != 0


class FakeQMP(threading.Thread):
    """Answers QMP commands on 'path', recording them, as QEMU would"""

    def __init__(self, path: Path, replies=None):
        super().__init__(daemon=True)
        self.commands: list[dict] = []
        self.replies = replies or {}
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen()
        self.start()

    def run(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn, conn.makefile("rw") as stream:
                stream.write(json.dumps({"QMP": {"version": {}}}) + "\n")
                stream.flush()
                for line in stream:
                    msg = json.loads(line)
                    self.commands.append(msg)
                    if msg["execute"] == "device_del":
                        device = msg["arguments"]["id"]
                        event = {"event": "DEVICE_DELETED", "data": {"device": device}}
                        stream.write(json.dumps(event) + "\n")
                    reply = "" if msg["execute"] == "human-monitor-command" else {}
                    reply = self.replies.get(msg["execute"], reply)
                    stream.write(json.dumps({"return": reply}) + "\n")
                    stream.flush()

    def executed(self):
        return [msg for msg in self.commands if msg["execute"] != "qmp_capabilities"]


def test_qmp_event_before_reply(tmp_path):
    fake = FakeQMP(tmp_path / "qmp.sock")
    with QMP(tmp_path / "qmp.sock", timeout=5) as qmp:
        assert qmp.execute("device_del", {"id": "x"}) == {"return": {}}
        assert qmp.wait_event("DEVICE_DELETED", "x", timeout=1)
        assert not qmp.wait_event("DEVICE_DELETED", "y", timeout=0.2)
    fake.listener.close()


def test_plug_and_unplug_qemu_device(tmp_path):
    fake = FakeQMP(tmp_path / "qmp.sock")
    topo = Topology(SWITCHED)

    assert rig.plug(tmp_path, topo, "nvme1") == 0
    assert rig.unplug(tmp_path, topo, "nvme1", timeout=1) == 0
    assert fake.executed() == [
        {
            "execute": "human-monitor-command",
            "arguments": {
                "command-line": "device_add nvme,serial=cafe,id=nvme1,bus=nvme1_rp"
            },
        },
        {"execute": "device_del", "arguments": {"id": "nvme1"}},
    ]
    fake.listener.close()


def test_plug_and_unplug_vfio_user_device(tmp_path):
    fake = FakeQMP(tmp_path / "qmp.sock")
    topo = Topology(SWITCHED)

    assert rig.plug(tmp_path, topo, "gpu0") == 0
    assert rig.unplug(tmp_path, topo, "gpu0", timeout=1) == 0

    added = fake.executed()[0]
    assert added["execute"] == "device_add"
    assert added["arguments"]["socket"] == {"type": "unix", "path": "/tmp/gpu0.sock"}
    assert added["arguments"]["x-msg-timeout"] == 1000
    fake.listener.close()


def test_unplug_that_times_out(tmp_path, monkeypatch):
    fake = FakeQMP(tmp_path / "qmp.sock")
    monkeypatch.setattr(QMP, "wait_event", lambda *args: False)

    assert rig.unplug(tmp_path, Topology(SWITCHED), "nvme1", timeout=0) != 0
    fake.listener.close()


def test_guest_args():
    args = rig.guest_args(
        Topology(SWITCHED), {"system_args": {"kwa": {"m": "6G"}}}, Path("/g")
    )
    joined = " ".join(args)

    assert "memory-backend-memfd,id=rig_mem,size=6G,share=on" in joined
    assert args[-2:] == ["-qmp", "unix:/g/qmp.sock,server,nowait"]
    assert "bus=sw_dn0" in joined and "id=nvm0" in joined
    assert "gpu0" not in joined, "a boot = false device is not there at boot"


def test_guest_args_without_servers_have_no_shared_memory():
    topo = Topology({"devices": [SWITCHED["devices"][2]]})
    assert "-object" not in rig.guest_args(topo, {}, Path("/g"))


def test_plug_takes_the_device_out_when_a_child_fails(tmp_path, monkeypatch):
    fake = FakeQMP(tmp_path / "qmp.sock")
    guest_config = {
        "devices": [
            {
                "name": "n",
                "provider": "qemu",
                "boot": False,
                "device": "nvme,serial=x",
                "children": ["nvme-ns,drive=d,nsid=1"],
            }
        ]
    }
    replies = iter(
        [{"return": ""}, {"return": "Error: Bus 'n' does not support hotplugging"}]
    )
    execute = QMP.execute

    def fail_child(qmp, command, arguments=None):
        if command == "human-monitor-command":
            execute(qmp, command, arguments)
            return next(replies)
        return execute(qmp, command, arguments)

    monkeypatch.setattr(QMP, "execute", fail_child)

    assert rig.plug(tmp_path, Topology(guest_config), "n") != 0
    assert fake.executed()[-1] == {"execute": "device_del", "arguments": {"id": "n"}}
    fake.listener.close()


def test_server_names_another_servers_pid(tmp_path):
    cijoe = make_cijoe(tmp_path)
    gpu = fake_server_dev(tmp_path)
    nic = {**gpu, "name": "nic", "socket": str(tmp_path / "nic.sock")}
    nic["server"] = gpu["server"] + " {pid:d}"

    assert Server(cijoe, tmp_path, nic).command() is None, "d is not running yet"

    topo = Topology({"devices": [gpu, nic]})
    assert rig.start_servers(cijoe, topo, tmp_path) == 0
    gpu_pid = Server(cijoe, tmp_path, gpu).pid()
    assert Server(cijoe, tmp_path, nic).command().endswith(f" {gpu_pid}")

    rig.stop_servers(cijoe, topo, tmp_path)
    assert not Server(cijoe, tmp_path, gpu).pid()
    assert not Server(cijoe, tmp_path, nic).pid()


def test_create_images(tmp_path):
    image = tmp_path / "sub" / "a.img"
    dev = {"name": "a", "provider": "qemu", "image": {"path": str(image), "size": "1M"}}
    topo = Topology({"devices": [dev]})

    assert rig.create_images(make_cijoe(tmp_path), topo) == 0
    assert image.stat().st_size == 1 << 20

    image.write_bytes(b"kept")
    assert rig.create_images(make_cijoe(tmp_path), topo) == 0
    assert image.read_bytes() == b"kept", "an existing image is left alone"


def test_rig_from_config_ignores_the_rig_variable(tmp_path, monkeypatch):
    cijoe = make_cijoe(tmp_path)
    cijoe.config.options = {"rig": keyed(SWITCHED)}
    monkeypatch.setenv("RIG", "cluster-node-emulated")

    topo = rig.Topology.from_config(cijoe)
    assert [dev["name"] for dev in topo.devices] == ["nvme0", "gpu0", "nvme1"]


def test_device_on_no_bus():
    subsys = {"name": "s", "provider": "qemu", "bus": False, "device": "nvme-subsys"}
    ctrl = {"name": "c", "provider": "qemu", "device": "nvme,subsys=s"}
    topo = Topology({"devices": [subsys, ctrl]})

    assert topo.port_args() == [
        "-device",
        "pcie-root-port,id=c_rp,chassis=1,slot=0",
    ]
    assert topo.boot_device_args() == [
        "-device",
        "nvme-subsys,id=s",
        "-device",
        "nvme,subsys=s,id=c,bus=c_rp",
    ]

    with pytest.raises(ValueError):
        Topology({"devices": [{**subsys, "children": ["nvme-ns"]}]})


def test_boot_args_are_shell_quoted():
    dev = {
        "name": "n",
        "provider": "qemu",
        "device": "nvme",
        "drives": ["id=d,file=/a b.img"],
        "children": ["nvme-ns,drive=d,fdp.ruhs=0;5"],
    }
    topo = Topology({"devices": [dev]})

    assert topo.drive_args() == ["-drive", "'id=d,file=/a b.img'"]
    assert topo.boot_device_args()[-1] == "'nvme-ns,drive=d,fdp.ruhs=0;5,id=n_c0,bus=n'"


RIGS_DIR = Path(__file__).parents[2] / "rigs"
RIGS = sorted(RIGS_DIR.glob("*.toml"))


@pytest.mark.parametrize("path", RIGS, ids=[p.stem for p in RIGS])
def test_shipped_rigs_parse(path):
    Topology(tomllib.loads(path.read_text())["rig"]).port_args()


def test_storage_node_keeps_its_bdfs():
    path = Path(__file__).parents[2] / "rigs" / "storage-node-emulated.toml"
    topo = Topology(tomllib.loads(path.read_text())["rig"])

    ports = [a.split(",")[1] for a in topo.port_args() if a.startswith("pcie-root")]
    assert ports == [f"id=nvme{i}_rp" for i in range(11)]


def test_compute_node_has_its_nvme_on_the_switchs_first_port():
    path = Path(__file__).parents[2] / "rigs" / "compute-node-emulated.toml"
    topo = Topology(tomllib.loads(path.read_text())["rig"])

    assert [a for a in topo.port_args() if a != "-device"][:3] == [
        "pcie-root-port,id=p2p_rp,chassis=1,slot=0",
        "x3130-upstream,id=p2p_up,bus=p2p_rp",
        "xio3130-downstream,id=p2p_dn0,bus=p2p_up,chassis=2,slot=0",
    ]
    assert topo.device("nvme0")["port"] == "p2p_dn0"
    assert not topo.device("gpu0").get("boot", True)


def test_gpu_rigs_have_the_same_nvme():
    def nvme0(name):
        path = Path(__file__).parents[2] / "rigs" / f"{name}.toml"
        dev = Topology(tomllib.loads(path.read_text())["rig"]).device("nvme0")
        return dev["device"], dev["children"], dev["image"]["size"]

    assert nvme0("cluster-node-emulated") == nvme0("compute-node-emulated")


def test_devices_listed_as_an_array_are_refused():
    with pytest.raises(ValueError):
        rig.Topology({"devices": [{"name": "a", "provider": "qemu"}]})


def test_a_layered_config_changes_one_field_and_appends_devices(tmp_path):
    from cijoe.cli.cli import create_combined_toml

    layer = tmp_path / "layer.toml"
    layer.write_text(
        '[rig.devices.nic0]\nserver = "other {socket}"\n'
        '[rig.devices.extra]\nprovider = "qemu"\ndevice = "nvme"\n'
    )
    combined = tmp_path / "combined.toml"
    create_combined_toml([RIGS_DIR / "cluster-node-emulated.toml", layer], combined)

    topo = rig.Topology(tomllib.loads(combined.read_text())["rig"])
    assert [d["name"] for d in topo.devices] == ["gpu0", "nic0", "nvme0", "extra"]
    assert topo.device("nic0")["server"] == "other {socket}"
    assert topo.device("nic0")["port"] == "p2p_dn0", "the rest of nic0 is kept"


PCI = "/sys/devices/pci0000:00/0000:00:03.0"
GUEST_LISTING = f"""\
ns nvme1n1 {PCI}/0000:01:00.0/nvme/nvme1/nvme1n1 1
ns ng1n1 {PCI}/0000:01:00.0/nvme/nvme1/ng1n1 1
ns ng0n1 /sys/devices/pci0000:00/0000:00:04.0/0000:02:00.0/nvme/nvme0/ng0n1 1
ns nvme2n1 /sys/devices/virtual/nvme-subsystem/nvme-subsys2/nvme2n1 1
ns ng2n1 /sys/devices/virtual/nvme-subsystem/nvme-subsys2/ng2n1
subsys nvme-subsys1 {PCI}/0000:01:00.0/nvme/nvme1
subsys nvme-subsys2 /sys/devices/pci0000:00/0000:00:05.0/0000:03:00.0/nvme/nvme2
"""


def test_guest_nvme_by_bdf():
    ctrls = rig.guest_nvme(GUEST_LISTING)

    assert ctrls["0000:01:00.0"] == {
        "controller": "nvme1",
        "namespaces": [{"nsid": 1, "bdev": "/dev/nvme1n1", "cdev": "/dev/ng1n1"}],
    }
    assert ctrls["0000:02:00.0"]["namespaces"] == [{"nsid": 1, "cdev": "/dev/ng0n1"}]
    shared = ctrls["0000:03:00.0"]
    assert shared["controller"] == "nvme2"
    assert {"nsid": 1, "bdev": "/dev/nvme2n1"} in shared["namespaces"]
    assert {"cdev": "/dev/ng2n1"} in shared["namespaces"], "no nsid is left out"


def test_guest_devices_reports_where_they_are(tmp_path):
    def pcidev(qdev_id, bus, devices=()):
        dev = {"qdev_id": qdev_id, "bus": bus, "slot": 0, "function": 0}
        if devices:
            dev["pci_bridge"] = {"devices": list(devices)}
        return dev

    tree = [{"devices": [pcidev("nvme0_rp", 0, [pcidev("nvme0", 1)]), pcidev("", 0)]}]
    FakeQMP(tmp_path / "qmp.sock", {"query-pci": tree})
    addresses = rig.pci_addresses(tmp_path)
    assert addresses == {"nvme0_rp": "0000:00:00.0", "nvme0": "0000:01:00.0"}

    topo = Topology(
        {
            "devices": [
                {"name": "nvme0", "provider": "qemu", "labels": ["nvm"]},
                {"name": "gone", "provider": "qemu", "boot": False},
            ]
        }
    )
    report = rig.guest_devices(topo, addresses, rig.guest_nvme(GUEST_LISTING))
    assert list(report) == ["nvme0"], "a device not in the guest is not reported"
    assert report["nvme0"]["labels"] == ["nvm"]
    assert report["nvme0"]["controller"] == "nvme1"


def test_guest_listing_command_is_valid_shell():
    subprocess.run(["bash", "-n", "-c", rig.GUEST_NVME], check=True)


@pytest.mark.parametrize("path", RIGS, ids=[p.stem for p in RIGS])
def test_shipped_rigs_label_their_nvme(path):
    topo = Topology(tomllib.loads(path.read_text())["rig"])
    for dev in topo.devices:
        if dev.get("device", "").startswith("nvme,") or "kvssd" in dev.get(
            "server", ""
        ):
            assert dev.get("labels"), dev["name"]


def fake_sysfs(root: Path):
    """A sysfs with an NVMe drive bound to nvme, a bridge and a device already on
    vfio-pci, all in IOMMU group 5, and a drive in no group"""

    devices = root / "bus" / "pci" / "devices"
    drivers = root / "bus" / "pci" / "drivers"
    group = root / "kernel" / "iommu_groups" / "5" / "devices"
    for name in ("nvme", "vfio-pci", "pcieport"):
        (drivers / name).mkdir(parents=True)
        (drivers / name / "unbind").write_text("")
    group.mkdir(parents=True)
    (root / "bus" / "pci" / "drivers_probe").write_text("")

    for bdf, cls, driver in [
        ("0000:02:00.0", "0x010802", "nvme"),
        ("0000:00:01.1", "0x060400", "pcieport"),
        ("0000:02:00.1", "0x010802", "vfio-pci"),
    ]:
        dev = root / "devices" / bdf
        dev.mkdir(parents=True)
        (dev / "class").write_text(cls + "\n")
        (dev / "driver_override").write_text("")
        (dev / "driver").symlink_to(drivers / driver)
        (dev / "iommu_group").symlink_to(group.parent)
        (devices / bdf).parent.mkdir(parents=True, exist_ok=True)
        (devices / bdf).symlink_to(dev)
        (group / bdf).symlink_to(dev)
    (root / "devices" / "0000:02:00.0" / "nvme" / "nvme3" / "nvme3n1").mkdir(
        parents=True
    )

    lone = root / "devices" / "0000:03:00.0"
    lone.mkdir(parents=True)
    (devices / "0000:03:00.0").symlink_to(lone)

    return root


def test_passthru_group_leaves_bridges_alone(tmp_path):
    sysfs = fake_sysfs(tmp_path)
    assert rig.group_members("0000:02:00.0", sysfs) == ["0000:02:00.0", "0000:02:00.1"]


class Recorder:
    """A cijoe whose run_local records the commands, answering with 'output'"""

    class State:
        def __init__(self, output):
            self._output = output

        def output(self):
            return self._output

    def __init__(self, output="", err=0):
        self.commands = []
        self.reply = (err, Recorder.State(output))

    def run_local(self, cmd):
        self.commands.append(cmd)
        return self.reply


@pytest.mark.parametrize("name", ["devbind", "iommu"])
def test_tool_runs_from_the_package_dependencies(name):
    ran = subprocess.run(
        shlex.split(rig.tool(name)) + ["--version"], capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr
    assert "0.4.0" in ran.stdout + ran.stderr


def test_bind_vfio_binds_with_devbind_what_is_not_on_vfio(tmp_path):
    sysfs = fake_sysfs(tmp_path)
    cijoe = Recorder()

    assert rig.bind_vfio(cijoe, "0000:02:00.0", sysfs) == ["0000:02:00.0"]
    assert cijoe.commands == [f"{rig.tool('devbind')} --bind vfio-pci --device 0000:02:00.0"]
    assert rig.bind_vfio(Recorder(err=1), "0000:02:00.0", sysfs) is None


def test_vfio_node_is_made_from_sysfs_when_missing(tmp_path, monkeypatch):
    sysfs = fake_sysfs(tmp_path / "sys")
    made = []
    monkeypatch.setattr(rig.os, "mknod", lambda *args: made.append(args))

    assert rig.vfio_node("0000:02:00.0", sysfs, tmp_path / "dev") is None
    (sysfs / "class" / "vfio" / "5").mkdir(parents=True)
    (sysfs / "class" / "vfio" / "5" / "dev").write_text("236:1\n")
    node = tmp_path / "dev" / "vfio" / "5"
    assert rig.vfio_node("0000:02:00.0", sysfs, tmp_path / "dev") == node
    assert made == [(node, 0o660 | stat.S_IFCHR, os.makedev(236, 1))]

    node.write_text("")
    assert rig.vfio_node("0000:02:00.0", sysfs, tmp_path / "dev") == node
    assert len(made) == 1


@pytest.mark.parametrize(
    "mode,groups,ok",
    [
        ("pt", "groups:    40 group(s) at /sys/kernel/iommu_groups\n", True),
        ("strict", "groups:    40 group(s) at /sys/kernel/iommu_groups\n", True),
        ("unset", "groups:    75 group(s) at /sys/kernel/iommu_groups\n", True),
        ("unset", "", False),
        ("off-for-vfio", "groups:    40 group(s) at /sys/kernel/iommu_groups\n", False),
        ("off-for-uio", "", False),
    ],
)
def test_iommu_on_from_iommu_show(mode, groups, ok):
    shown = f"cmdline:   BOOT_IMAGE=/vmlinuz ro\nmode:      {mode}\n{groups}"
    cijoe = Recorder(shown)

    assert (rig.iommu_problems(cijoe) == []) is ok
    assert cijoe.commands == [f"{rig.tool('iommu')} show"]


@pytest.mark.parametrize(
    "host,mounted,holder,expect",
    [
        ("0000:02:00.0", "", None, []),
        ("0000:02:00.0", "/dev/nvme3n1p2 / ext4 rw 0 0", None, ["has /dev/nvme3n1 mounted"]),
        ("0000:02:00.0", "", "holders/md127", ["held by md127"]),
        ("0000:02:00.0", "", "nvme3n1p1/holders/dm-0", ["held by dm-0"]),
        ("02:00.0", "", None, ["is not a BDF"]),
        ("0000:09:00.0", "", None, ["no PCI device"]),
        ("0000:03:00.0", "", None, ["in no IOMMU group"]),
    ],
    ids=["ok", "mounted", "md", "dm-on-partition", "not-a-bdf", "missing", "no-iommu"],
)
def test_passthru_problems(tmp_path, host, mounted, holder, expect):
    sysfs = fake_sysfs(tmp_path / "sys")
    if holder:
        ns = sysfs / "devices" / "0000:02:00.0" / "nvme" / "nvme3" / "nvme3n1"
        (ns / holder).mkdir(parents=True)
    mounts = tmp_path / "mounts"
    mounts.write_text(mounted + "\n")

    dev = {"name": "nvme0", "provider": "vfio-pci", "host": host}
    problems = rig.passthru_problems(dev, sysfs, mounts)
    assert len(problems) == len(expect)
    for problem, part in zip(problems, expect):
        assert part in problem


def test_vfio_pci_device_args_and_plug(tmp_path):
    dev = {"name": "nvme0", "provider": "vfio-pci", "host": "0000:02:00.0"}
    topo = Topology({"devices": [dev, {**dev, "name": "late", "boot": False}]})

    args = topo.boot_device_args()
    assert len(args) == 2, "only the device present at boot"
    assert json.loads(args[1][1:-1]) == {
        "driver": "vfio-pci",
        "id": "nvme0",
        "bus": "nvme0_rp",
        "host": "0000:02:00.0",
    }
    assert topo.served() == [], "a passed-through device has no server"

    fake = FakeQMP(tmp_path / "qmp.sock")
    assert rig.plug(tmp_path, topo, "late") == 0
    assert fake.executed()[0]["arguments"]["driver"] == "vfio-pci"


@pytest.mark.parametrize(
    "serial,ssh_err,expect", [(True, 1, 0), (False, 0, 0), (False, 1, 110)]
)
def test_guest_start_takes_ssh_for_a_guest_without_a_serial_login(
    tmp_path, monkeypatch, serial, ssh_err, expect
):
    import argparse

    import rig_guest_start

    class Guest:
        def __init__(self, cijoe, config, name):
            self.guest_config, self.guest_path = {}, tmp_path

        def start(self, extra_args):
            return 0

        def is_up(self, timeout):
            return serial

    class Ssh:
        config = argparse.Namespace(options={})

        def run(self, cmd):
            return ssh_err, None

    monkeypatch.setattr(rig_guest_start, "Guest", Guest)
    args = argparse.Namespace(guest_name="g", timeout=1)
    assert rig_guest_start.main(args, Ssh()) == expect


# As a FreeBSD 14.4 guest on storage-node-passthru printed them
FREEBSD_NVME = """\
hostb0@pci0:0:0:0:\tclass=0x060000 rev=0x00 hdr=0x00 vendor=0x8086 device=0x29c0
pcib1@pci0:0:2:0:\tclass=0x060400 rev=0x00 hdr=0x01 vendor=0x1b36 device=0x000c
nvme0@pci0:1:0:0:\tclass=0x010802 rev=0x00 hdr=0x00 vendor=0x144d device=0xa80c
nvme1@pci0:10:0:0:\tclass=0x010802 rev=0x02 hdr=0x00 vendor=0x1b36 device=0x0010
scbus2 on ahcich2 bus 0:
<QEMU QEMU DVD-ROM 2.5+>           at scbus2 target 0 lun 0 (pass0,cd0)
<>                                 at scbus2 target -1 lun ffffffff ()
scbus6 on nvme0 bus 0:
<Samsung SSD 990 PRO 2TB 5B2QJXD7>  at scbus6 target 0 lun 1 (pass1,nda0)
<>                                 at scbus6 target -1 lun ffffffff ()
scbus-1 on xpt0 bus 0:
<>                                 at scbus-1 target -1 lun ffffffff (xpt0)
 nvme0: Samsung SSD 990 PRO 2TB
    nvme0ns1 (1907729MB)
 nvme1: QEMU NVMe Ctrl
    nvme1ns1 (1024MB)
"""


def test_guest_nvme_freebsd_by_bdf():
    assert rig.guest_nvme_freebsd(FREEBSD_NVME) == {
        "0000:01:00.0": {
            "controller": "nvme0",
            "namespaces": [{"nsid": 1, "cdev": "/dev/nvme0ns1", "bdev": "/dev/nda0"}],
        },
        "0000:0a:00.0": {
            "controller": "nvme1",
            "namespaces": [{"nsid": 1, "cdev": "/dev/nvme1ns1"}],
        },
    }
