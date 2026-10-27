#!/usr/bin/env python3
"""
Check that the host has every tool the environments rely on
===========================================================

Runs each tool where cijoe runs, whatever the transport, and fails naming
every one that is missing or does not run. The host container's build runs
this, so an image without one is never published, and so does qualify, so
the report says what the host had.

Retargetable: false
-------------------
"""

import errno
import logging as log
import subprocess

# Each with a command that runs it without side effects
TOOLS = {
    "qemu-system-x86_64": ["qemu-system-x86_64", "--version"],
    "qemu-img": ["qemu-img", "--version"],
    "qemu-nbd": ["qemu-nbd", "--version"],
    "cijoe": ["cijoe", "--version"],
    "pipx": ["pipx", "--version"],
    "oras": ["oras", "version"],
    "mdadm": ["mdadm", "--version"],
    "vfu_kvssd": ["vfu_kvssd", "--version"],
    "rocjitsu": ["/opt/rocjitsu/bin/rocjitsu", "--version"],
    "rocjitsu-vram": ["test", "-x", "/opt/rocjitsu/bin/rocjitsu-vram"],
    "rj-ip-discovery": ["test", "-x", "/opt/rocjitsu/bin/rj-ip-discovery"],
    "rocm-ernic": ["/opt/rocm-ernic/bin/rocm-ernic", "--help"],
}


def main(args, cijoe):
    """Run every tool, report each, fail if any did not run"""

    missing = []
    for name, command in TOOLS.items():
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            missing.append(name)
            log.error(f"{name}: {exc}")
            continue
        if run.returncode:
            missing.append(name)
            log.error(f"{name}: '{' '.join(command)}' exited {run.returncode}")
            continue
        first = (run.stdout or run.stderr).strip().splitlines()
        print(f"{name}: {first[0] if first else 'ok'}")

    if missing:
        log.error(f"the host lacks: {', '.join(missing)}")
        return errno.ENOENT
    return 0
