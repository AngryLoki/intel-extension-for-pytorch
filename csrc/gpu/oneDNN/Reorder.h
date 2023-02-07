#pragma once

#include <ATen/ATen.h>

#include <ATen/record_function.h>
#include <oneDNN/Runtime.h>
#include <runtime/Utils.h>
#include <tensor/Context.h>
#include <utils/LRUCache.h>
#include "Utils.h"

#include <oneapi/dnnl/dnnl.hpp>

using namespace dnnl;
using namespace xpu::dpcpp;
using namespace at::AtenIpexTypeXPU;

namespace xpu {
namespace oneDNN {

struct ReorderAttr {
 public:
  ReorderAttr(bool is_group = false)
      : pattr_(primitive_attr()), sc_(std::vector<float>()) {}

 public:
  void set_src_sc_and_zp(
      int scmask,
      std::vector<float> sc,
      int zpmask,
      std::vector<int> zp) {
    pattr_.set_output_scales(scmask, sc);
    pattr_.set_zero_points(DNNL_ARG_SRC, zpmask, zp);
    sc_ = sc;
  }

  void set_dst_sc_and_zp(
      int scmask,
      std::vector<float> sc,
      int zpmask,
      std::vector<int> zp) {
    pattr_.set_output_scales(scmask, sc);
    pattr_.set_zero_points(DNNL_ARG_DST, zpmask, zp);
    sc_ = sc;
  }

  bool is_quant() const {
    return !sc_.empty();
  }

  std::vector<float> sc() const {
    return sc_;
  }

  primitive_attr pattr() const {
    return pattr_;
  }

 private:
  primitive_attr pattr_;
  std::vector<float> sc_;
};

static inline void reorder(
    const Tensor& src,
    Tensor& dst,
    const ReorderAttr& rattr = ReorderAttr()) {
  RECORD_FUNCTION("dnnl_reorder", std::vector<c10::IValue>({src}));

  if (dst.is_same(src))
    return;

  auto engine =
      GpuEngineManager::Instance().get_engine({kXPU, current_device()});
  auto strm = GpuStreamManager::Instance().get_stream();

  auto check_group_and_create_plain_md = [](const Tensor& src,
                                            const Tensor& dst) -> memory::desc {
    if (src.ndimension() == dst.ndimension()) {
      return memory::desc(
          get_onednn_dims(src),
          get_onednn_dtype_include_double(src),
          get_onednn_strides(src));
    } else if (
        ((src.ndimension() == dst.ndimension() - 1) &&
         (src.size(0) == dst.size(0) * dst.size(1))) ||
        ((src.ndimension() == dst.ndimension() + 1) &&
         (dst.size(0) == src.size(0) * src.size(1)))) {
      // group tensor
      return memory::desc(
          get_onednn_dims(dst),
          get_onednn_dtype_include_double(src),
          get_onednn_strides(dst.contiguous()));
    } else {
      TORCH_CHECK(0, "invalid src/dst dimension in oneDNN reorder ...");
    }
  };

  auto src_ctx = DPCPPTensorContext::get_tensor_ctx(src);
  memory::desc src_md = src_ctx.is_plain()
      ? check_group_and_create_plain_md(src, dst)
      : src_ctx.meta();
  auto src_mem = dpcpp_onednn_memory(src_md, engine, src.data_ptr());

  auto dst_ctx = DPCPPTensorContext::get_tensor_ctx(dst);
  memory::desc dst_md = dst_ctx.is_plain()
      ? memory::desc(
            get_onednn_dims(dst),
            get_onednn_dtype_include_double(dst),
            get_onednn_strides(dst))
      : dst_ctx.meta();
  auto dst_mem = dpcpp_onednn_memory(dst_md, engine, dst.data_ptr());

  primitive prim;
  if (rattr.is_quant()) {
    auto pattr = rattr.pattr();
    prim = dnnl::reorder(src_mem, dst_mem, pattr);
  } else {
    prim = dnnl::reorder(src_mem, dst_mem);
  }

  DPCPP_ONEDNN_EXEC(
      prim, strm, {{DNNL_ARG_SRC, src_mem}, {DNNL_ARG_DST, dst_mem}});
}

static inline void reorder_copy(const Tensor& src, Tensor& dst) {
  RECORD_FUNCTION("reorder_copy", std::vector<c10::IValue>({src}));
  xpu::oneDNN::reorder(src, dst);
}

} // namespace oneDNN
} // namespace xpu
