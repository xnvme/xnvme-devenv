// FP32 128x128x128 GEMM on the device, every element checked against the CPU.
#include <hip/hip_runtime.h>
#include <cmath>
#include <cstdio>
#include <vector>

#define CHECK(x)                                                              \
	do {                                                                      \
		hipError_t e = (x);                                                   \
		if (e != hipSuccess) {                                                \
			printf("sgemm-hip: FAIL %s: %s\n", #x, hipGetErrorString(e));      \
			return 1;                                                         \
		}                                                                     \
	} while (0)

__global__ void sgemm(int n, const float *a, const float *b, float *c)
{
	int row = blockIdx.y * blockDim.y + threadIdx.y;
	int col = blockIdx.x * blockDim.x + threadIdx.x;
	if (row >= n || col >= n)
		return;
	float acc = 0.0f;
	for (int k = 0; k < n; k++)
		acc += a[row * n + k] * b[k * n + col];
	c[row * n + col] = acc;
}

int main()
{
	const int n = 128;
	std::vector<float> a(n * n), b(n * n), c(n * n), ref(n * n);
	for (int i = 0; i < n * n; i++) {
		a[i] = (float)((i * 7) % 13) / 13.0f;
		b[i] = (float)((i * 5) % 11) / 11.0f;
	}
	for (int r = 0; r < n; r++)
		for (int col = 0; col < n; col++) {
			float acc = 0.0f;
			for (int k = 0; k < n; k++)
				acc += a[r * n + k] * b[k * n + col];
			ref[r * n + col] = acc;
		}

	hipDeviceProp_t prop;
	CHECK(hipGetDeviceProperties(&prop, 0));
	printf("sgemm-hip: device %s arch %s\n", prop.name, prop.gcnArchName);

	float *da, *db, *dc;
	size_t bytes = n * n * sizeof(float);
	CHECK(hipMalloc(&da, bytes));
	CHECK(hipMalloc(&db, bytes));
	CHECK(hipMalloc(&dc, bytes));
	CHECK(hipMemcpy(da, a.data(), bytes, hipMemcpyHostToDevice));
	CHECK(hipMemcpy(db, b.data(), bytes, hipMemcpyHostToDevice));
	CHECK(hipMemset(dc, 0, bytes));
	dim3 block(16, 16), grid(n / 16, n / 16);
	hipLaunchKernelGGL(sgemm, grid, block, 0, 0, n, da, db, dc);
	CHECK(hipGetLastError());
	CHECK(hipDeviceSynchronize());
	CHECK(hipMemcpy(c.data(), dc, bytes, hipMemcpyDeviceToHost));

	int failures = 0;
	float max_err = 0.0f;
	for (int i = 0; i < n * n; i++) {
		float err = fabsf(c[i] - ref[i]);
		max_err = fmaxf(max_err, err);
		if (err > 1e-3f * fmaxf(1.0f, fabsf(ref[i])))
			failures++;
	}
	printf("sgemm-hip: m=%d n=%d k=%d max_absolute_error=%g failures=%d\n", n, n,
	       n, max_err, failures);
	printf("sgemm-hip: %s\n", failures ? "FAIL" : "PASS");
	return failures ? 1 : 0;
}
