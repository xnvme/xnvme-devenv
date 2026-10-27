#!/usr/bin/env python3
"""
Kill a qemu guest and its rig's servers
=======================================

Kills the guest, as qemu.guest_kill does, then the servers of the rig's
devices. The guest goes first, so it never sees its devices vanish. Does not
fail when the guest or a server is not running.

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


def main(args, cijoe):
    """Kill the guest, then its rig's servers"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not guest_name:
        log.error("missing argument: guest_name")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    err = guest.kill()
    rig.stop_servers(cijoe, rig.Topology.from_config(cijoe), guest.guest_path)

    return err
