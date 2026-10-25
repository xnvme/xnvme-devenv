#!/usr/bin/env python3
"""
Start a qemu-guest with an emulated RDMA NIC and room for an emulated GPU
=========================================================================

Starts the rocjitsu GPU server and the rocm-ernic NIC server on the initiator,
then the guest with the NIC attached. The GPU is not attached here: amdgpu has
to be loaded with its options before the GPU appears, so rocjitsu_gpu_attach
hot-plugs it later over the guest's QMP socket, onto the port left free here.

The NIC, the GPU's port and an emulated NVMe drive are the downstream ports
of one emulated PCIe switch. The guest kernel allows peer-to-peer DMA between devices behind one
switch, whereas between root ports it would need the q35 host bridge on its
whitelist, and amdgpu would quietly fall back to system memory.

The NIC reaches the GPU's VRAM through the server's '--peer-memory': QEMU
announces the GPU's BAR to the NIC server as a DMA region without a file, so
the NIC server maps rocjitsu's VRAM memfd itself, named by 'rocjitsu-vram'.

Both servers are installed in the host container, rocjitsu from rocjitsu-host
under /opt/rocjitsu and rocm-ernic under /opt/rocm-ernic, from releases of the
same revisions as the guest's firmware and drivers.

Retargetable: false
-------------------
"""
import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path

from cijoe.qemu.wrapper import Guest

# Bus names the attach step hot-plugs the GPU onto and checks against
NIC_PORT = "dn1"
GPU_PORT = "dn2"
NVME_PORT = "dn3"


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, default=None)
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for the guest to reach its login prompt.",
    )


def start_detached(cijoe, command, socket, log_path):
    """Start 'command' detached, logging to 'log_path'; wait for 'socket'"""

    cijoe.run_local(f"rm -f {socket}")
    err, _ = cijoe.run_local(
        f"setsid {command} > {log_path} 2>&1 < /dev/null & echo started"
    )
    if err:
        return err

    return cijoe.run_local(
        f"for _ in $(seq 1 300); do [ -S {socket} ] && break; sleep 0.1; done; "
        f"[ -S {socket} ]"
    )[0]


def setup_rocjitsu(cijoe):
    """Start the rocjitsu server, replacing any stale one on its socket, since
    QEMU's vfio-user session does not survive one; return the socket and the
    path of its VRAM memfd, or None on failure"""

    binary = cijoe.getconf("rocjitsu.bin")
    socket = cijoe.getconf("rocjitsu.socket")
    log_path = Path(socket).with_suffix(".log")

    err, _ = cijoe.run_local(f"[ -x {binary} ]")
    if err:
        log.error(f"no {binary}; install the rocjitsu-host package")
        return None

    # Anchored, so as not to match the shell running the command
    pattern = f"^{binary} .*--vfio-socket {socket}"
    cijoe.run_local(f"pkill -f '{pattern}' || true")
    err = start_detached(
        cijoe,
        f"{binary} --config {cijoe.getconf('rocjitsu.config')} --vfio-socket {socket}",
        socket,
        log_path,
    )
    if err:
        log.error(f"rocjitsu did not create socket {socket}; see {log_path}")
        return None

    err, state = cijoe.run_local(
        f"{cijoe.getconf('rocjitsu.vram_bin')} "
        f"\"$(pgrep -nf '{pattern}')\""
    )
    if err:
        log.error("could not find rocjitsu's VRAM memfd")
        return None

    vram = state.output().strip()
    log.info(f"rocjitsu listening on {socket}, VRAM at {vram}")
    return socket, vram


def setup_rocm_ernic(cijoe, peer_memory):
    """As in xNVMe's xnvme_guest_start_nvme, with the server pointed at the
    GPU's VRAM; return its socket path, or None on failure"""

    binary = cijoe.getconf("rocm_ernic.bin")
    err, _ = cijoe.run_local(f"[ -x {binary} ]")
    if err:
        log.error(f"no {binary}; install the rocm-ernic package")
        return None

    socket = cijoe.getconf("rocm_ernic.socket", "/tmp/vfio-user-rocm-ernic.sock")
    backend = cijoe.getconf("rocm_ernic.backend", "loopback")
    log_level = cijoe.getconf("rocm_ernic.log_level", "warn")
    log_path = Path(socket).with_suffix(".log")

    cijoe.run_local(f"pkill -f '^{binary} --socket {socket}' || true")
    err = start_detached(
        cijoe,
        f"{binary} --socket {socket} --backend '{backend}' "
        f"--log-level {log_level} --peer-memory '{peer_memory}'",
        socket,
        log_path,
    )
    if err:
        log.error(f"rocm-ernic did not create socket {socket}; see {log_path}")
        return None

    log.info(f"rocm-ernic ({backend}) listening on {socket}")
    return socket


def main(args, cijoe):
    """Start the servers and the guest"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not guest_name:
        log.error("missing config value(qemu.guest_name)")
        return 1

    guest = Guest(cijoe, cijoe.config, guest_name)

    rocjitsu = setup_rocjitsu(cijoe)
    if not rocjitsu:
        return errno.EIO
    _, vram = rocjitsu

    ernic_socket = setup_rocm_ernic(
        cijoe, f"{vram}:{cijoe.getconf('rocjitsu.vram_aperture')}"
    )
    if not ernic_socket:
        return errno.EIO

    # Both servers need guest memory they can map, hence the shared memfd. The
    # JSON spec is single-quoted because cijoe runs qemu via a shell.
    mem = guest.guest_config.get("system_args", {}).get("kwa", {}).get("m", "6G")
    ernic_dev = (
        '{"driver":"vfio-user-pci","id":"nic0","bus":"' + NIC_PORT + '",'
        '"rombar":0,"socket":{"type":"unix","path":"' + ernic_socket + '"}}'
    )
    # A scratch drive for the kernel checks; its content does not matter
    nvme_img = guest.guest_path / "nvme0.img"
    err, _ = cijoe.run_local(f"truncate -s 1G {nvme_img}")
    if err:
        return err

    extra_args = [
        "-machine",
        "memory-backend=mem0",
        "-object",
        f"memory-backend-memfd,id=mem0,size={mem},share=on",
        "-device",
        "pcie-root-port,id=rp1,chassis=1,slot=1",
        "-device",
        "x3130-upstream,id=up1,bus=rp1",
        "-device",
        f"xio3130-downstream,id={NIC_PORT},bus=up1,chassis=2,slot=0",
        "-device",
        f"xio3130-downstream,id={GPU_PORT},bus=up1,chassis=3,slot=1",
        "-device",
        f"xio3130-downstream,id={NVME_PORT},bus=up1,chassis=4,slot=2",
        "-device",
        f"'{ernic_dev}'",
        "-drive",
        f"file={nvme_img},if=none,id=nvm0,format=raw,cache=none",
        "-device",
        f"nvme,serial=xnvme-images,drive=nvm0,bus={NVME_PORT}",
        "-qmp",
        f"unix:{guest.guest_path / 'qmp.sock'},server,nowait",
    ]

    err = guest.start(extra_args=extra_args)
    if err:
        log.error(f"guest.start() : err({err})")
        return err

    if not guest.is_up(timeout=args.timeout):
        log.error("guest.is_up() : False")
        return errno.EAGAIN

    return 0
