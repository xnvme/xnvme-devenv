#!/usr/bin/env python3
"""
Report the rig's devices present in its running guest
=====================================================

Writes '[guest.devices.<device>]' for each of the rig's devices in the running
guest, to 'guest-devices.toml' in the guest's path and in the step's output: its
'labels', its 'bdf' as QEMU reports it, its 'transport' when it has one, and,
for an NVMe controller, the 'controller' and 'namespaces', '{nsid, bdev, cdev}',
the guest named, a Linux or a FreeBSD guest. A consumer passes the file as one more config and builds its
device list from it. Run it after the guest is up and root can log in, and
again after a plug or unplug.

With 'expect_controllers', fails unless that many NVMe controllers are present.

Retargetable: false
-------------------
"""

import errno
import logging as log
import os
from argparse import ArgumentParser
from pathlib import Path

import rig

from cijoe.core.resources import dict_to_tomlfile
from cijoe.qemu.wrapper import Guest


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, default=None)
    parser.add_argument(
        "--expect_controllers",
        type=int,
        default=-1,
        help="Fail unless this many NVMe controllers are present; -1 checks none.",
    )


def main(args, cijoe):
    """Report the rig's devices present in its running guest"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not guest_name:
        log.error("missing argument: guest_name")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    if not guest.is_running():
        log.error(f"guest {guest_name} is not running")
        return errno.ENODEV

    err, state = cijoe.run("uname -s")
    freebsd = not err and "FreeBSD" in state.output()
    err, state = cijoe.run(rig.GUEST_NVME_FREEBSD if freebsd else rig.GUEST_NVME)
    if err:
        log.error(f"listing the guest's NVMe devices failed: err({err})")
        return err
    reader = rig.guest_nvme_freebsd if freebsd else rig.guest_nvme

    report = rig.guest_devices(
        rig.Topology.from_config(cijoe),
        rig.pci_addresses(guest.guest_path),
        reader(state.output()),
    )
    config = {"guest": {"devices": report}}
    dict_to_tomlfile(config, Path(guest.guest_path) / "guest-devices.toml")
    dict_to_tomlfile(
        config,
        Path(os.path.join(cijoe.output_path, cijoe.output_ident))
        / "guest-devices.toml",
    )

    controllers = [name for name, dev in report.items() if "controller" in dev]
    if args.expect_controllers >= 0 and len(controllers) != args.expect_controllers:
        log.error(
            f"expected {args.expect_controllers} NVMe controllers; found {controllers}"
        )
        return errno.ENODEV

    return 0
