# xnvme-images

Test environments for xNVMe's CI, released in matched pairs: a guest image
and the host container that serves its emulated devices, under one tag.

| Image | What |
| ----- | ---- |
| guest | nosi's `ubuntu-2610-rocm` with our kernel and the guest drivers built in: `ionic-ernic`, the amdgpu GC 12.1 fixes, upcie's modules, the rocjitsu firmware stubs and options |
| host  | nosi's `ubuntu-2604-docker`, which has stock QEMU and cijoe, with the vfio-user servers: rocm-ernic, rocjitsu, vfu_kvssd |

The two are released together because what breaks silently crosses between
them: the rocm-ernic server and its guest driver, rocjitsu and its firmware,
our kernel and the modules built for it. xNVMe pins one tag for both.

Nothing is built here from source. The pieces are debs released by their
producers, listed by URL and sha256 in one lock file: the `xnvme-ci/*` tags
of the safl forks of rocm-ernic, rocm-systems and linux, upcie, and
vfio-user-kvssd. A release boots the guest under the host container and
qualifies it, a GEMM on the emulated GPU and NIC to VRAM DMA among the
checks, before either image is published.

The guest is for CI only and is never flashed onto a machine, so it may carry
emulation settings that nosi must not. Machines that cannot reboot into a
new kernel install the producers' DKMS debs on their own kernel instead.

## Making a release

`make host`, then `make derive qualify` inside the host container, as
`.github/workflows/ci.yml` runs them on every push. A `v*` tag publishes the
host as `ghcr.io/xnvme/xnvme-images/host:<tag>` and the guest, with the
lock it was made from, as `ghcr.io/xnvme/xnvme-images/guest-ubuntu-2610:<tag>`.

## Licensing

BSD-3-Clause (`LICENSE`), except where a file says otherwise:
`guest/tests/rdma_p2p.hip` is GPL-2.0-or-later, following rocm-ernic's tests.
