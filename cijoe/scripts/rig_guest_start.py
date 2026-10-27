#!/usr/bin/env python3
"""
Start a qemu guest with its rig
===============================

Binds the host devices the rig passes through to vfio-pci, creates the images
the rig's devices name, starts their servers, in the order the devices are
listed, then the guest with the rig's PCIe topology and the devices present at
boot, and waits for it to reach its login prompt. See rig.py for what a rig
describes.

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
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for the guest to reach its login prompt.",
    )


def main(args, cijoe):
    """Start the rig's servers, then the guest with its devices"""

    guest_name = args.guest_name or cijoe.getconf("qemu.default_guest")
    if not guest_name:
        log.error("missing argument: guest_name")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    topo = rig.Topology.from_config(cijoe)

    err = (
        rig.prepare_passthru(cijoe, topo)
        or rig.create_images(cijoe, topo)
        or rig.start_servers(cijoe, topo, guest.guest_path)
    )
    if not err:
        err = guest.start(
            extra_args=rig.guest_args(topo, guest.guest_config, guest.guest_path)
        )
    if err:
        log.error(f"starting the rig failed: err({err})")
        rig.stop_servers(cijoe, topo, guest.guest_path)
        return err

    if not guest.is_up(timeout=args.timeout):
        log.error("the guest did not reach its login prompt")
        return errno.ETIMEDOUT

    return 0
