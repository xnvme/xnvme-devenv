#!/usr/bin/env python3
"""
Turn a running nosi guest into an xnvme-images guest
====================================================

Installs the guest debs: our kernel, linux-image-7.3.0-xnvme<n> and its
headers, and the DKMS packages, which build their modules for it here, once
per release rather than per test run. Makes our kernel the one GRUB boots,
reboots into it, and checks that it is running and that every DKMS module
took the place of the kernel's own. Installs the guest checks,
rocjitsu-guest-check and its sources, from guest/tests.

Retargetable: false
-------------------
"""
import errno
import logging as log
import time
from argparse import ArgumentParser
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2] / "guest" / "tests"
REMOTE = "/tmp/xnvme-images"
MODULES = ["amdgpu", "ionic", "ionic_rdma", "dmabuf_import", "iommu_map_pa"]


def reboot(cijoe, timeout=300):
    """Reboot the guest and return once it answers again, or False"""
    cijoe.run("systemd-run --on-active=2 systemctl reboot")
    time.sleep(15)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not cijoe.run("true")[0]:
            return True
        time.sleep(5)
    return False

def add_args(parser: ArgumentParser):
    parser.add_argument("--debs", type=str, default=None)


def main(args, cijoe):
    """Install the guest debs and checks, and verify the modules"""

    debs = sorted(Path(args.debs or cijoe.getconf("derive.debs")).glob("*.deb"))
    if not debs:
        log.error("no guest debs to install")
        return errno.ENOENT

    kernel = cijoe.getconf("derive.kernel", "")
    if not any(d.name.startswith(f"linux-image-{kernel}_") for d in debs):
        log.error(f"no linux-image deb for the kernel {kernel!r} among the guest debs")
        return errno.EINVAL

    err, _ = cijoe.run(f"mkdir -p {REMOTE}")
    if err:
        return err
    for path in [*debs, *sorted(TESTS.iterdir())]:
        if not cijoe.put(str(path), f"{REMOTE}/{path.name}"):
            return errno.EIO

    err, _ = cijoe.run(
        "export DEBIAN_FRONTEND=noninteractive && apt-get -qy update && "
        "apt-get -qy install --no-install-recommends liburing-dev "
        f"{' '.join(f'{REMOTE}/{d.name}' for d in debs)}"
    )
    if err:
        return err

    err, _ = cijoe.run(
        f"install -D -m 0755 {REMOTE}/rocjitsu-guest-check /usr/local/bin/rocjitsu-guest-check && "
        f"install -D -m 0644 -t /usr/share/rocjitsu-guest-tests {REMOTE}/*.hip {REMOTE}/*.c"
    )
    if err:
        return err

    # The stock kernel stays installed, and sorts first; name ours as the default
    err, _ = cijoe.run(
        "sed -i '/^GRUB_DEFAULT=/d' /etc/default/grub && "
        f"echo 'GRUB_DEFAULT=\"Advanced options for Ubuntu>Ubuntu, with Linux {kernel}\"' "
        ">> /etc/default/grub && update-grub"
    )
    if err:
        return err
    if not reboot(cijoe):
        log.error(f"the guest did not come back after rebooting into {kernel}")
        return errno.ETIMEDOUT
    err, running = cijoe.run("uname -r")
    if err or running.output().strip() != kernel:
        log.error(f"the guest runs {running.output().strip()!r}, not {kernel}")
        return errno.EINVAL

    # Every DKMS module must now be the one modprobe finds for our kernel
    err, _ = cijoe.run(
        f"dkms status; for m in {' '.join(MODULES)}; do "
        'p=$(modinfo -n "$m") || exit 1; echo "$m: $p"; '
        'case "$p" in */updates/dkms/*) ;; *) echo "$m is not the DKMS build"; exit 1 ;; esac; '
        "done"
    )
    if err:
        log.error("a DKMS module did not take the kernel's place")
        return err

    # Ubuntu's ROCr insists on a hotswap library for rocjitsu's gfx1250 A0 agent,
    # which nothing ships, and hsa_init fails without it; skipping it is what
    # lets any HIP program run, so every login gets it, cijoe's SSH included
    err, _ = cijoe.run(
        "grep -qx HSA_HOTSWAP_DISABLE=1 /etc/environment || "
        "echo HSA_HOTSWAP_DISABLE=1 >> /etc/environment"
    )
    if err:
        return err

    # The guest checks compile on first use; the toolchain must be there
    err, _ = cijoe.run("command -v hipcc")
    if err:
        log.error("no hipcc in the guest; rocjitsu-guest-check cannot run")
        return err

    return cijoe.run(f"rm -rf {REMOTE} && apt-get clean")[0]
