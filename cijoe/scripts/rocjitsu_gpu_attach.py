#!/usr/bin/env python3
"""
Attach the emulated GPU to a running guest
==========================================

Hot-plugs the rocjitsu GPU over the guest's QMP socket, onto the port
rocjitsu_guest_start left free, and returns once the guest has a KFD and a
render node. The guest image carries amdgpu and its options already, so udev
loads it for the new device; QMP rather than HMP, since HMP's device_add
cannot express the vfio-user socket.

Retargetable: false
-------------------
"""
import errno
import json
import logging as log
import socket
import time
from argparse import ArgumentParser

import rocjitsu_guest_start

from cijoe.qemu.wrapper import Guest


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, default=None)
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for the guest's KFD and render node.",
    )


def qmp(path, command, arguments):
    """Run one QMP command on the socket at 'path'; return its reply"""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(path))
        stream = sock.makefile("rw")
        stream.readline()  # the greeting
        for msg in (
            {"execute": "qmp_capabilities"},
            {"execute": command, "arguments": arguments},
        ):
            stream.write(json.dumps(msg) + "\n")
            stream.flush()
            while True:
                reply = json.loads(stream.readline())
                if "event" not in reply:
                    break
        return reply


def main(args, cijoe):
    """Hot-plug the GPU and wait for the guest to bring it up"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    guest = Guest(cijoe, cijoe.config, guest_name)

    reply = qmp(
        guest.guest_path / "qmp.sock",
        "device_add",
        {
            "driver": "vfio-user-pci",
            "id": "gpu0",
            "bus": rocjitsu_guest_start.GPU_PORT,
            "rombar": 0,
            "socket": {"type": "unix", "path": cijoe.getconf("rocjitsu.socket")},
        },
    )
    if "return" not in reply:
        log.error(f"device_add: {reply}")
        return errno.EIO

    began = time.time()
    while time.time() - began < args.timeout:
        err, _ = cijoe.run("test -e /dev/kfd && ls /dev/dri | grep -q renderD")
        if not err:
            return 0
        time.sleep(5)

    log.error("the guest has no KFD and render node; see its dmesg")
    cijoe.run("dmesg | grep -i amdgpu | tail -40")
    return errno.ETIMEDOUT
