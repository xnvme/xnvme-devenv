#!/usr/bin/env python3
"""
Power the derived guest off and export its disk
===============================================

Shuts the guest down cleanly, so the image holds a consistent filesystem,
then flattens its boot image, an overlay on the nosi base, into the qcow2
qualify boots and the .img.gz a release publishes. The machine-id is cleared
first, so every guest booted from the image gets its own.

Retargetable: false
-------------------
"""

import errno
import logging as log
import time
from argparse import ArgumentParser
from pathlib import Path

from cijoe.qemu.wrapper import Guest


def add_args(parser: ArgumentParser):
    parser.add_argument("--guest_name", type=str, default="derive")
    parser.add_argument("--timeout", type=int, default=300)


def main(args, cijoe):
    """Shut the guest down, then export its disk"""

    guest = Guest(cijoe, cijoe.config, args.guest_name)
    qcow2 = Path(
        cijoe.getconf("system-imaging.images.xnvme-devenv-guest-x86_64.disk.path")
    )
    gz = Path(cijoe.getconf("derive.gz_path"))

    cijoe.run("truncate -s 0 /etc/machine-id && sync")
    # Detached, so the shell returns before sshd goes away
    cijoe.run("systemd-run --on-active=2 systemctl poweroff")

    began = time.time()
    while guest.is_running() and time.time() - began < args.timeout:
        time.sleep(5)
    if guest.is_running():
        log.error("the guest did not power off")
        return errno.ETIMEDOUT

    qcow2.parent.mkdir(parents=True, exist_ok=True)
    err, _ = cijoe.run_local(f"qemu-img convert -O qcow2 {guest.boot_img} {qcow2}")
    if err:
        return err
    raw = gz.with_suffix("")
    err, _ = cijoe.run_local(
        f"qemu-img convert -O raw {qcow2} {raw} && gzip -1 -f {raw} && ls -l {gz}"
    )
    return err
