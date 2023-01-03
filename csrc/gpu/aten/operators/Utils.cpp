#include "Utils.h"
#include <c10/util/BFloat16.h>
#include <c10/util/Half.h>
#include <oneDNN/oneDNN.h>
#include <runtime/Utils.h>
#include <utils/DPCPP.h>
#include "utils/CustomOperatorRegistration.h"

using namespace xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

template <typename dst_dt, typename src_dt>
DPCPP_HOST void dtype_convert_by_scalar(
    dst_dt* dst,
    const src_dt* src,
    size_t n_elements) {
  auto& dpcpp_queue = dpcppGetCurrentQueue();
  auto dev_id = dpcppGetDeviceIdOfCurrentQueue();
  auto total_threads = dpcppMaxWorkGroupSize(dev_id);

  auto cgf = DPCPP_Q_CGF(cgh) {
    cgh.parallel_for(sycl::range<1>(total_threads), [=](sycl::item<1> itemId) {
      auto in_ptr = src;
      auto out_ptr = dst;
      auto id = itemId.get_id(0);
      for (auto i = id; i < n_elements; i += itemId.get_range()[0])
        out_ptr[i] = (dst_dt)in_ptr[i];
    });
  };

  // launch kernel
  DPCPP_Q_SUBMIT(dpcpp_queue, cgf);
}

#define DT_CONVERT_EXPLICIT_INST(DST_T, SRC_T)      \
  template DPCPP_HOST void dtype_convert_by_scalar( \
      DST_T* dst, const SRC_T* src, size_t n_elements);

#define DT_CONVERT_EXPLICIT_BI_INST(T1, T2) \
  DT_CONVERT_EXPLICIT_INST(T1, T2);         \
  DT_CONVERT_EXPLICIT_INST(T2, T1);

DT_CONVERT_EXPLICIT_INST(float, float);
DT_CONVERT_EXPLICIT_BI_INST(int, int64_t);
DT_CONVERT_EXPLICIT_BI_INST(at::Half, float);
DT_CONVERT_EXPLICIT_BI_INST(at::BFloat16, float);

DPCPP_HOST sycl::event dpcpp_q_barrier(sycl::queue& q) {
#ifdef USE_QUEUE_BARRIER
  return q.ext_oneapi_submit_barrier();
#else
  auto cgf = [&](sycl::handler& cgh) { cgh.single_task([=]() {}); };
  return q.submit(cgf);
#endif
}

DPCPP_HOST sycl::event dpcpp_q_barrier(
    sycl::queue& q,
    std::vector<sycl::event>& events) {
#ifdef USE_QUEUE_BARRIER
  return q.ext_oneapi_submit_barrier(events);
#else
  auto cgf = [&](sycl::handler& cgh) {
    cgh.depends_on(events);
    cgh.single_task([=]() {});
  };
  return q.submit(cgf);
#endif
}

bool check_onednn_layout(const at::Tensor& input) {
  return xpu::oneDNN::is_onednn_layout(input);
}

} // namespace AtenIpexTypeXPU
} // namespace at

namespace {
IPEX_LIBRARY_FRAGMENT() {
  IPEX_OP_REGISTER(
      "check_onednn_layout.xpu", at::AtenIpexTypeXPU::check_onednn_layout);
}
} // namespace
