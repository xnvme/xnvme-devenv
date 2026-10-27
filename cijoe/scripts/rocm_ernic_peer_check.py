#!/usr/bin/env python3
"""
Check that the NIC moved data through the GPU's VRAM
====================================================

A guest-side test passing shows the bytes arrived, not where they went: had
the guest kernel refused peer-to-peer DMA, amdgpu would have placed the buffer
in system memory and the test would pass all the same. The rocm-ernic server
logs "peer memory ... served DMA at" the first time it serves a transfer from
its '--peer-memory' mapping, so this fails unless that line is in its log,
which is beside the socket of the rig's device 'device_name'.

Retargetable: false
-------------------
"""

import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path

import rig


def add_args(parser: ArgumentParser):
    parser.add_argument("--device_name", type=str, default="nic0")


def main(args, cijoe):
    """Fail unless the rocm-ernic server served DMA from the GPU's VRAM"""

    try:
        dev = rig.Topology.from_config(cijoe).device(args.device_name)
    except KeyError:
        log.error(f"the rig has no device {args.device_name}")
        return errno.ENODEV
    log_path = Path(dev["socket"]).with_suffix(".log")

    err, _ = cijoe.run_local(f"grep 'peer memory' {log_path}")
    if err:
        log.error(f"no peer memory lines in {log_path}")
        return errno.EIO

    err, _ = cijoe.run_local(f"grep -q 'peer memory .* served DMA at' {log_path}")
    if err:
        log.error("the NIC never touched the GPU's VRAM")
        return errno.EIO

    return 0
