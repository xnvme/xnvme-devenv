#!/usr/bin/env python3
"""
Fetch the debs the lock pins for one side
=========================================

Downloads every 'lock.deb' entry whose side is 'host' or 'guest' into
'dest', checking each against its sha256; a mismatch is fatal, and nothing
unchecked is left behind. It fetches where cijoe runs, whatever the transport.

Retargetable: false
-------------------
"""

import errno
import hashlib
import logging as log
import shutil
import urllib.parse
import urllib.request
from argparse import ArgumentParser
from pathlib import Path


def add_args(parser: ArgumentParser):
    parser.add_argument("--side", choices=["host", "guest"], required=True)
    parser.add_argument("--dest", type=str, required=True)


def main(args, cijoe):
    """Fetch and check the debs of 'args.side' into 'args.dest'"""

    debs = [deb for deb in cijoe.getconf("lock.deb", []) if deb["side"] == args.side]
    if not debs:
        log.error(f"the lock pins no {args.side} debs")
        return errno.ENOENT

    output = Path(args.dest)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    for deb in debs:
        name = urllib.parse.unquote(deb["url"].rsplit("/", 1)[1])
        with urllib.request.urlopen(deb["url"], timeout=300) as resp:
            data = resp.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != deb["sha256"]:
            log.error(f"{name}: sha256 {digest}, the lock pins {deb['sha256']}")
            shutil.rmtree(output, ignore_errors=True)
            return errno.EINVAL
        (output / name).write_bytes(data)
        log.info(f"fetched {output / name}")

    return 0
