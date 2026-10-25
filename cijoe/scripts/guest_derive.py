#!/usr/bin/env python3
"""
Turn a running nosi guest into an xnvme-images guest
====================================================

Installs the guest debs, whose DKMS packages build their modules for the
guest's kernel here, once per release rather than per test run, and checks
that the modules took the place of the kernel's own. Installs the guest
checks, rocjitsu-guest-check and its sources, from guest/tests.

nosi's ubuntu-2610-rocm up to 2026.W43 carried its own build of ionic-ernic,
registered with DKMS under the name the ionic-ernic package uses; it is
removed first when present. Images after that carry none, and the step finds
nothing to remove.

Retargetable: false
-------------------
"""
import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2] / "guest" / "tests"
REMOTE = "/tmp/xnvme-images"

LEGACY_ERNIC = (
    "if [ -d /usr/src/ionic-ernic-7.3 ] && ! dpkg -S /usr/src/ionic-ernic-7.3 >/dev/null 2>&1; then "
    "dkms remove ionic-ernic/7.3 --all || true; "
    "rm -rf /usr/src/ionic-ernic-7.3 /opt/rocm-ernic /usr/local/share/rocm-ernic "
    "/etc/udev/rules.d/99-rocm-ernic.rules /etc/modules-load.d/rocm-ernic.conf; "
    "echo 'removed the image-built ionic-ernic'; fi"
)


def add_args(parser: ArgumentParser):
    parser.add_argument("--debs", type=str, default=None)


def main(args, cijoe):
    """Install the guest debs and checks, and verify the modules"""

    debs = sorted(Path(args.debs or cijoe.getconf("derive.debs")).glob("*.deb"))
    if not debs:
        log.error("no guest debs to install")
        return errno.ENOENT

    err, _ = cijoe.run(f"mkdir -p {REMOTE} && {LEGACY_ERNIC}")
    if err:
        return err
    for path in [*debs, *sorted(TESTS.iterdir())]:
        if not cijoe.put(str(path), f"{REMOTE}/{path.name}"):
            return errno.EIO

    err, _ = cijoe.run(
        "export DEBIAN_FRONTEND=noninteractive && apt-get -qy update && "
        f"apt-get -qy install --no-install-recommends {' '.join(f'{REMOTE}/{d.name}' for d in debs)}"
    )
    if err:
        return err

    err, _ = cijoe.run(
        f"install -D -m 0755 {REMOTE}/rocjitsu-guest-check /usr/local/bin/rocjitsu-guest-check && "
        f"install -D -m 0644 -t /usr/share/rocjitsu-guest-tests {REMOTE}/*.hip"
    )
    if err:
        return err

    # Every DKMS module must now be the one modprobe finds for this kernel
    err, state = cijoe.run(
        "dkms status; for m in amdgpu ionic ionic_rdma; do "
        'p=$(modinfo -n "$m") || exit 1; echo "$m: $p"; '
        'case "$p" in */updates/dkms/*) ;; *) echo "$m is not the DKMS build"; exit 1 ;; esac; '
        "done"
    )
    if err:
        log.error("a DKMS module did not take the kernel's place")
        return err

    # The guest checks compile on first use; the toolchain must be there
    err, _ = cijoe.run("command -v hipcc")
    if err:
        log.error("no hipcc in the guest; rocjitsu-guest-check cannot run")
        return err

    return cijoe.run(f"rm -rf {REMOTE} && apt-get clean")[0]
