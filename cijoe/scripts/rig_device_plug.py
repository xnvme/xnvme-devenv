#!/usr/bin/env python3
"""
Plug a rig's device into its running guest
==========================================

Adds the device 'device_name', one of the rig's devices with 'boot = false',
to the running guest over its QMP socket. Returns once QEMU has added it;
waiting for the guest to bring it up is the task's to do, as only it knows what
that looks like.

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
    """Plug a rig's device into its running guest"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not (guest_name and args.device_name):
        log.error("missing argument: guest_name or device_name")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    if not guest.is_running():
        log.error(f"guest {guest_name} is not running")
        return errno.ENODEV

    try:
        return rig.plug(
            guest.guest_path, rig.Topology.from_config(cijoe), args.device_name
        )
    except KeyError:
        log.error(f"the rig has no device {args.device_name}")
        return errno.ENODEV
