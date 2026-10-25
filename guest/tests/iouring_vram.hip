// SPDX-License-Identifier: BSD-3-Clause
//
// io_uring I/O between a block device and GPU VRAM: hipMalloc memory is
// exported as a dma-buf, registered as a fixed buffer for the device, written
// to the device with WRITE_FIXED, and read back into the zeroed buffer with
// READ_FIXED. The device DMAs straight into and out of VRAM, so every byte is
// checked through HIP, and the test fails if KFD evicted the process's queues
// on the way, as pinning a buffer the device cannot reach would.
//
//   hipcc --offload-arch=gfx1250 -o iouring_vram iouring_vram.hip -luring -lhsa-runtime64
//   ./iouring_vram /dev/nvme0n1
#include <hip/hip_runtime.h>
#include <hsa/hsa.h>
#include <hsa/hsa_ext_amd.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <liburing.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <vector>

#define SZ (1024 * 1024)

// From the series' include/uapi/linux/io_uring.h, which the guest's headers
// predate; as in iouring_dmabuf.c
#define XI_RSRC_UPDATE_EXTENDED (1U << 1)
#define XI_REGBUF_TYPE_DMABUF 2

struct xi_rsrc_update2 {
	uint32_t offset;
	uint32_t flags;
	uint64_t data;
	uint64_t tags;
	uint32_t nr;
	uint32_t resv2;
};

struct xi_regbuf_desc {
	uint32_t type;
	uint32_t flags;
	uint64_t size;
	uint64_t uaddr;
	int32_t dmabuf_fd;
	int32_t target_fd;
	uint64_t resv[6];
};

static int fail(const char *what, int err)
{
	fprintf(stderr, "iouring_vram: %s: %s\n", what, strerror(err));
	return 1;
}

static int io(struct io_uring *ring, int op, int fd)
{
	for (;;) {
		struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
		struct io_uring_cqe *cqe;
		int res;

		if (op == IORING_OP_WRITE_FIXED)
			io_uring_prep_write_fixed(sqe, fd, NULL, SZ, 0, 0);
		else
			io_uring_prep_read_fixed(sqe, fd, NULL, SZ, 0, 0);
		io_uring_submit(ring);
		if (io_uring_wait_cqe(ring, &cqe))
			return -EIO;
		res = cqe->res;
		io_uring_cqe_seen(ring, cqe);
		if (res != -EAGAIN)
			return res;
	}
}

// As in rdma_p2p.hip: milliseconds KFD has held this process's queues evicted
static long evicted_ms(void)
{
	char pat[96];
	glob_t g;
	long sum = 0;
	snprintf(pat, sizeof(pat), "/sys/class/kfd/kfd/proc/%d/stats_*/evicted_ms", getpid());
	if (glob(pat, 0, nullptr, &g))
		return -1;
	for (size_t i = 0; i < g.gl_pathc; i++) {
		FILE *f = fopen(g.gl_pathv[i], "r");
		long ms = 0;
		if (f && fscanf(f, "%ld", &ms) == 1)
			sum += ms;
		if (f)
			fclose(f);
	}
	globfree(&g);
	return sum;
}

int main(int argc, char **argv)
{
	struct io_uring ring;
	uint8_t *gpu;
	int dev, fd, err;
	uint64_t off;

	if (argc != 2) {
		fprintf(stderr, "usage: %s <block device>\n", argv[0]);
		return 2;
	}
	dev = open(argv[1], O_RDWR | O_DIRECT);
	if (dev < 0)
		return fail(argv[1], errno);

	// Exported and registered before HIP creates any queue, as rdma_p2p does
	if (hipMalloc(&gpu, SZ) != hipSuccess)
		return fail("hipMalloc", ENOMEM);
	if (hsa_amd_portable_export_dmabuf(gpu, SZ, &fd, &off) != HSA_STATUS_SUCCESS || off)
		return fail("hsa_amd_portable_export_dmabuf", EINVAL);

	err = io_uring_queue_init(8, &ring, 0);
	if (err)
		return fail("io_uring_queue_init", -err);
	err = io_uring_register_buffers_sparse(&ring, 1);
	if (err)
		return fail("io_uring_register_buffers_sparse", -err);
	struct xi_regbuf_desc desc = {.type = XI_REGBUF_TYPE_DMABUF, .dmabuf_fd = fd, .target_fd = dev};
	struct xi_rsrc_update2 up = {.flags = XI_RSRC_UPDATE_EXTENDED, .data = (uintptr_t)&desc, .nr = 1};
	err = syscall(__NR_io_uring_register, ring.ring_fd, IORING_REGISTER_BUFFERS_UPDATE, &up,
		      sizeof(up));
	if (err != 1)
		return fail("registering the VRAM dma-buf for the device", err < 0 ? errno : EINVAL);

	std::vector<uint8_t> pat(SZ), got(SZ);
	for (size_t i = 0; i < SZ; i++)
		pat[i] = (uint8_t)(i * 7 + 3 + i / 4096);
	if (hipMemcpy(gpu, pat.data(), SZ, hipMemcpyHostToDevice) != hipSuccess)
		return fail("hipMemcpy to VRAM", EIO);

	err = io(&ring, IORING_OP_WRITE_FIXED, dev);
	if (err != SZ)
		return fail("WRITE_FIXED from VRAM", err < 0 ? -err : EIO);
	if (hipMemset(gpu, 0, SZ) != hipSuccess || hipDeviceSynchronize() != hipSuccess)
		return fail("hipMemset", EIO);
	err = io(&ring, IORING_OP_READ_FIXED, dev);
	if (err != SZ)
		return fail("READ_FIXED into VRAM", err < 0 ? -err : EIO);
	if (hipMemcpy(got.data(), gpu, SZ, hipMemcpyDeviceToHost) != hipSuccess)
		return fail("hipMemcpy from VRAM", EIO);
	if (memcmp(got.data(), pat.data(), SZ)) {
		fprintf(stderr, "iouring_vram: VRAM differs after the read\n");
		return 1;
	}
	long ms = evicted_ms();
	if (ms != 0) {
		fprintf(stderr, "iouring_vram: KFD evicted the queues (evicted_ms %ld)\n", ms);
		return 1;
	}
	printf("iouring_vram: %d bytes from VRAM to %s and back into VRAM: PASS\n", SZ, argv[1]);
	return 0;
}
