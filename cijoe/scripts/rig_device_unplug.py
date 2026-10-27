#!/usr/bin/env python3
"""
Unplug a rig's device from its running guest
============================================

Removes the device 'device_name' from the running guest over its QMP socket,
and waits for the guest to release it. Its server, if it has one, keeps running,
so it can be plugged again.

Retargetable: false
-------------------
"""

import errno
import logging as log
from argparse import ArgumentParser

import rig

from cijoe.qemu.wrapper import Guest


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, default=None)
    parser.add_argument("--device_name", type=str, help="Name of the device.")


def main(args, cijoe):
    """Unplug a rig's device from its running guest"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not (guest_name and args.device_name):
        log.error("missing argument: guest_name or device_name")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    if not guest.is_running():
        log.error(f"guest {guest_name} is not running")
        return errno.ENODEV

    try:
        return rig.unplug(
            guest.guest_path, rig.Topology.from_config(cijoe), args.device_name
        )
    except KeyError:
        log.error(f"the rig has no device {args.device_name}")
        return errno.ENODEV
