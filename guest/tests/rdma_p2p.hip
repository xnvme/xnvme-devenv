// SPDX-License-Identifier: GPL-2.0-or-later
//
// RDMA between host memory and GPU VRAM on one NIC: two RC QPs in one process,
// the second one's MR a dma-buf exported from hipMalloc memory. Every byte that
// lands in VRAM is read back through HIP and checked. The QP setup follows
// rocm-ernic's tests/rdma_verify.c.
//
//   hipcc --offload-arch=gfx1250 -o rdma_p2p rdma_p2p.hip -libverbs -lhsa-runtime64
//   ./rdma_p2p [device]
#include <hip/hip_runtime.h>
#include <hsa/hsa.h>
#include <hsa/hsa_ext_amd.h>
#include <infiniband/verbs.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define SZ (64 * 1024)

static int failures;

struct ep {
	ibv_pd *pd;
	ibv_cq *cq;
	ibv_qp *qp;
	ibv_mr *mr;
	uint8_t *addr; // host pointer, or the GPU pointer used as the MR's iova
};

static int ep_qp(ep *e, ibv_context *ctx)
{
	e->cq = ibv_create_cq(ctx, 16, nullptr, nullptr, 0);
	ibv_qp_init_attr ia = {};
	ia.send_cq = e->cq;
	ia.recv_cq = e->cq;
	ia.cap.max_send_wr = 16;
	ia.cap.max_recv_wr = 16;
	ia.cap.max_send_sge = 1;
	ia.cap.max_recv_sge = 1;
	ia.qp_type = IBV_QPT_RC;
	e->qp = e->cq ? ibv_create_qp(e->pd, &ia) : nullptr;
	return e->qp ? 0 : -1;
}

static int ep_connect(ep *e, uint32_t peer, ibv_gid gid)
{
	ibv_qp_attr a = {};
	a.qp_state = IBV_QPS_INIT;
	a.port_num = 1;
	a.qp_access_flags = IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ;
	if (ibv_modify_qp(e->qp, &a, IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
					     IBV_QP_ACCESS_FLAGS))
		return -1;
	ibv_qp_attr r = {};
	r.qp_state = IBV_QPS_RTR;
	r.path_mtu = IBV_MTU_1024;
	r.dest_qp_num = peer;
	r.max_dest_rd_atomic = 1;
	r.min_rnr_timer = 12;
	r.ah_attr.is_global = 1;
	r.ah_attr.port_num = 1;
	r.ah_attr.grh.dgid = gid;
	r.ah_attr.grh.hop_limit = 1;
	if (ibv_modify_qp(e->qp, &r, IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
					     IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
					     IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER))
		return -1;
	ibv_qp_attr s = {};
	s.qp_state = IBV_QPS_RTS;
	s.timeout = 14;
	s.retry_cnt = 7;
	s.rnr_retry = 7;
	s.max_rd_atomic = 1;
	return ibv_modify_qp(e->qp, &s, IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
						IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN |
						IBV_QP_MAX_QP_RD_ATOMIC);
}

static int post(ep *e, ibv_wr_opcode op, size_t off, uint32_t len, uint64_t raddr,
		uint32_t rkey)
{
	ibv_sge sge = {(uintptr_t)e->addr + off, len, e->mr->lkey};
	ibv_send_wr wr = {}, *bad;
	wr.sg_list = &sge;
	wr.num_sge = 1;
	wr.opcode = op;
	wr.send_flags = IBV_SEND_SIGNALED;
	wr.wr.rdma.remote_addr = raddr;
	wr.wr.rdma.rkey = rkey;
	return ibv_post_send(e->qp, &wr, &bad);
}

static int post_recv(ep *e, size_t off, uint32_t len)
{
	ibv_sge sge = {(uintptr_t)e->addr + off, len, e->mr->lkey};
	ibv_recv_wr wr = {}, *bad;
	wr.sg_list = &sge;
	wr.num_sge = 1;
	return ibv_post_recv(e->qp, &wr, &bad);
}

static void wait_cq(ep *e, const char *what)
{
	ibv_wc wc;
	for (int i = 0; i < 50000000; i++) {
		int n = ibv_poll_cq(e->cq, 1, &wc);
		if (n < 0)
			break;
		if (n == 0)
			continue;
		if (wc.status != IBV_WC_SUCCESS) {
			printf("FAIL %-12s completion %s\n", what, ibv_wc_status_str(wc.status));
			failures++;
		}
		return;
	}
	printf("FAIL %-12s no completion\n", what);
	failures++;
}

static void hip(hipError_t err, const char *what)
{
	if (err != hipSuccess) {
		fprintf(stderr, "%s: %s\n", what, hipGetErrorString(err));
		exit(1);
	}
}

static void check(const char *what, const uint8_t *got, const uint8_t *want, size_t len)
{
	for (size_t i = 0; i < len; i++)
		if (got[i] != want[i]) {
			printf("FAIL %-12s byte %zu: got 0x%02x want 0x%02x\n", what, i, got[i],
			       want[i]);
			failures++;
			return;
		}
	printf("ok   %-12s %zu bytes\n", what, len);
}

int main(int argc, char **argv)
{
	int num;
	ibv_device **list = ibv_get_device_list(&num);
	ibv_device *dev = nullptr;
	for (int i = 0; i < num; i++)
		if (argc < 2 || !strcmp(ibv_get_device_name(list[i]), argv[1]))
			dev = list[i];
	ibv_context *ctx = dev ? ibv_open_device(dev) : nullptr;
	ibv_gid gid;
	if (!ctx || ibv_query_gid(ctx, 1, 0, &gid)) {
		fprintf(stderr, "no RDMA device\n");
		return 1;
	}

	uint8_t *gpu;
	if (hipMalloc(&gpu, SZ) != hipSuccess) {
		fprintf(stderr, "hipMalloc failed\n");
		return 1;
	}
	int fd;
	uint64_t off;
	hsa_status_t st = hsa_amd_portable_export_dmabuf(gpu, SZ, &fd, &off);
	if (st != HSA_STATUS_SUCCESS) {
		fprintf(stderr, "hsa_amd_portable_export_dmabuf: %d\n", st);
		return 1;
	}

	int acc = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ;
	ep h = {}, g = {};
	h.pd = g.pd = ibv_alloc_pd(ctx);
	h.addr = (uint8_t *)aligned_alloc(4096, SZ);
	memset(h.addr, 0, SZ);
	h.mr = ibv_reg_mr(h.pd, h.addr, SZ, acc);
	g.addr = gpu;
	g.mr = ibv_reg_dmabuf_mr(g.pd, off, SZ, (uintptr_t)gpu, fd, acc);
	if (!h.mr || !g.mr) {
		fprintf(stderr, "MR registration failed (%s MR): %s\n", h.mr ? "dma-buf" : "host",
			strerror(errno));
		return 1;
	}
	// Only now touch the GPU. Pinning the buffer for the MR evicts the
	// process's queues, and under rocjitsu they do not come back, so the pin
	// has to happen before HIP has created any.
	if (hipMemset(gpu, 0, SZ) != hipSuccess || hipDeviceSynchronize() != hipSuccess) {
		fprintf(stderr, "hipMemset failed\n");
		return 1;
	}
	if (ep_qp(&h, ctx) || ep_qp(&g, ctx) || ep_connect(&h, g.qp->qp_num, gid) ||
	    ep_connect(&g, h.qp->qp_num, gid)) {
		fprintf(stderr, "QP setup failed\n");
		return 1;
	}

	std::vector<uint8_t> pat(SZ), got(SZ);
	for (size_t i = 0; i < SZ; i++)
		pat[i] = (uint8_t)(i * 7 + 3 + i / 4096);

	// Host -> VRAM, crossing pages and starting mid-page.
	memcpy(h.addr, pat.data(), SZ);
	post(&h, IBV_WR_RDMA_WRITE, 100, 20000, (uintptr_t)gpu + 100, g.mr->rkey);
	wait_cq(&h, "rdma-write");
	hip(hipMemcpy(got.data(), gpu, SZ, hipMemcpyDeviceToHost), "hipMemcpy");
	check("WRITE->VRAM", got.data() + 100, pat.data() + 100, 20000);

	// VRAM -> host.
	std::vector<uint8_t> pat2(SZ);
	for (size_t i = 0; i < SZ; i++)
		pat2[i] = (uint8_t)(i * 13 + 1);
	hip(hipMemcpy(gpu, pat2.data(), SZ, hipMemcpyHostToDevice), "hipMemcpy");
	memset(h.addr, 0, SZ);
	post(&h, IBV_WR_RDMA_READ, 0, 32768, (uintptr_t)gpu + 8192, g.mr->rkey);
	wait_cq(&h, "rdma-read");
	check("READ<-VRAM", h.addr, pat2.data() + 8192, 32768);

	// SEND into a receive posted on VRAM.
	hip(hipMemset(gpu, 0, SZ), "hipMemset");
	hip(hipDeviceSynchronize(), "hipDeviceSynchronize");
	memcpy(h.addr, pat.data(), 4096);
	post_recv(&g, 40960, 4096);
	post(&h, IBV_WR_SEND, 0, 4096, 0, 0);
	wait_cq(&h, "send");
	wait_cq(&g, "recv");
	hip(hipMemcpy(got.data(), gpu, SZ, hipMemcpyDeviceToHost), "hipMemcpy");
	check("SEND->VRAM", got.data() + 40960, pat.data(), 4096);

	printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "PASSED", failures,
	       failures == 1 ? "" : "s");
	return failures ? 1 : 0;
}
