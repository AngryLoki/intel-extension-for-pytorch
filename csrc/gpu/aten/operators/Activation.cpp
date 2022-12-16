#include <ATen/ATen.h>
#include <ATen/Functions.h>
#include <ATen/native/Activation.h>
#include <ATen/native/Resize.h>
#include "ATen/OpMathType.h"

#include <core/Generator.h>
#include <runtime/Utils.h>
#include <utils/DPCPP.h>
#include <utils/Macros.h>
#include "comm/ApplyUtils.h"

#include "comm/ATDispatch.h"
#include "comm/AccumulateType.h"
#include "comm/Numerics.h"
#include "comm/RegistrationDeclarations.h"

#include <oneDNN/oneDNN.h>
#include "DistributionTemplates.h"
#include "Loops.h"
#include "RandomEngine.h"

using namespace xpu::dpcpp::detail;
using namespace xpu::dpcpp;

namespace at {
namespace AtenIpexTypeXPU {

namespace impl {

template <typename scalar_t, int unroll_factor, typename F, typename item_t>
inline void rrelu_with_noise_kernel(
    item_t& item,
    int numel,
    PhiloxState philox_args,
    scalar_t* output,
    scalar_t* input,
    scalar_t* noise,
    double lower,
    double upper,
    const F& random_func) {
  auto thread_idx = item.get_local_id(0);
  auto thread_range = item.get_local_range(0);
  auto group_idx = item.get_group(0);
  auto group_range = item.get_group_range(0);

  auto seeds = philox_unpack(philox_args);
  int idx = group_idx * thread_range + thread_idx;
  randStatePhilox4_32_10_t state;
  rand_init(std::get<0>(seeds), idx, std::get<1>(seeds), &state);

  int range_stride = thread_range * group_range * unroll_factor;
  int rounded_size = ((numel - 1) / range_stride + 1) * range_stride;
  double range = upper - lower;

  for (int linear_index = idx; linear_index < rounded_size;
       linear_index += range_stride) {
    auto rand = random_func(&state);

    // ensure that (&rand.x)[ii] is safe
    static_assert(sizeof(rand) / sizeof(rand.x) == unroll_factor, "");

#pragma unroll
    for (int ii = 0; ii < unroll_factor; ii++) {
      int li = linear_index + thread_range * group_range * ii;
      if (li >= numel) {
        continue;
      }
      scalar_t r = static_cast<scalar_t>((&rand.x)[ii]);
      r = r * range + lower;
      if (input[li] <= 0) {
        output[li] = input[li] * r;
        noise[li] = r;
      } else {
        output[li] = input[li];
        noise[li] = static_cast<scalar_t>(1);
      }
    }
  }
}

template <typename scalar_t>
inline void _rrelu_with_noise_train(
    Tensor& output,
    const Tensor& input_,
    const Tensor& noise_,
    const Scalar& lower_,
    const Scalar& upper_,
    c10::optional<Generator> generator) {
  auto& sycl_queue = dpcppGetCurrentQueue();
  auto input = input_.contiguous();
  auto noise = noise_.contiguous();
  Tensor tmp_output = output.contiguous();

  int64_t numel = input.numel();
  auto execution_policy = calc_execution_policy(numel);

  auto counter_offset = std::get<0>(execution_policy);
  auto num_groups = std::get<1>(execution_policy);
  auto group_size = std::get<2>(execution_policy);

  auto gen = at::get_generator_or_default<DPCPPGeneratorImpl>(
      generator, getDefaultDPCPPGenerator());
  std::pair<uint64_t, uint64_t> seeds;
  {
    // See Note [Acquire lock when using random generators]
    std::lock_guard<std::mutex> lock(gen->mutex_);
    seeds = gen->philox_engine_inputs(counter_offset);
  }
  PhiloxState rng_engine_inputs(std::get<0>(seeds), std::get<1>(seeds));

  scalar_t* input_data = input.data_ptr<scalar_t>();
  scalar_t* noise_data = noise.data_ptr<scalar_t>();
  scalar_t* output_data = tmp_output.data_ptr<scalar_t>();

  double lower = lower_.to<double>();
  double upper = upper_.to<double>();

  if (std::is_same<scalar_t, double>::value) {
    auto cgf = DPCPP_Q_CGF(cgh) {
      auto kfn = DPCPP_Q_KFN(sycl::nd_item<1> item) {
        rrelu_with_noise_kernel<scalar_t, 2>(
            item,
            numel,
            rng_engine_inputs,
            output_data,
            input_data,
            noise_data,
            lower,
            upper,
            [](randStatePhilox4_32_10_t* state) {
              return rand_uniform2_double(state);
            });
      };
      cgh.parallel_for(
          sycl::nd_range<1>(num_groups * group_size, group_size), kfn);
    };
    DPCPP_Q_SUBMIT(sycl_queue, cgf);
  } else {
    // half and float
    auto cgf = DPCPP_Q_CGF(cgh) {
      auto kfn = DPCPP_Q_KFN(sycl::nd_item<1> item) {
        rrelu_with_noise_kernel<scalar_t, 4>(
            item,
            numel,
            rng_engine_inputs,
            output_data,
            input_data,
            noise_data,
            lower,
            upper,
            [](randStatePhilox4_32_10_t* state) {
              return rand_uniform4(state);
            });
      };
      cgh.parallel_for(
          sycl::nd_range<1>(num_groups * group_size, group_size), kfn);
    };
    DPCPP_Q_SUBMIT(sycl_queue, cgf);
  }

  if (!output.is_contiguous()) {
    output.copy_(tmp_output);
  }
}

inline void launch_prelu_kernel_share_weights(
    TensorIteratorBase& iter,
    const TensorBase& weight) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.input_dtype(),
      "prelu",
      [&] {
        const auto* weight_data = weight.data_ptr<scalar_t>();
        dpcpp_kernel_for_tensor_iter(
            iter, [weight_data](scalar_t input_val) -> scalar_t {
              return (input_val > 0) ? input_val : *weight_data * input_val;
            });
      });
}

inline void launch_prelu_kernel_multi_weights(
    const TensorBase& result,
    const TensorBase& input,
    const TensorBase& weight) {
  auto& sycl_queue = dpcppGetCurrentQueue();
  int64_t input_ndim = input.dim();
  TORCH_CHECK(input_ndim > 0, "Not allow zero-dim input tensor.");

  int64_t channel_size = 1; // channel_size default to 1
  int64_t input_stride0 = 1, input_stride1 = 1;

  if (input_ndim > 1) {
    channel_size = input.size(1); // channel is the 2nd dim of input
    auto strides = input.strides();
    input_stride0 = strides[0];
    input_stride1 = strides[1];
  }
  const int64_t weight_num = weight.numel();
  TORCH_CHECK(
      channel_size == weight_num,
      "Mismatch of parameter numbers and input channel size. Found parameter numbers = ",
      weight_num,
      " and channel size = ",
      channel_size,
      ".");

  // config to run cuda kernel
  int64_t input_numel = input.numel();
  auto group_size = std::min(
      static_cast<int64_t>(
          dpcppMaxWorkGroupSize(dpcppGetDeviceIdOfCurrentQueue())),
      input_numel);
  auto num_groups = (input_numel + group_size - 1) / group_size;
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      input.scalar_type(),
      "prelu",
      [&] {
        auto result_data = result.data_ptr<scalar_t>();
        auto input_data = input.data_ptr<scalar_t>();
        auto weight_data = weight.data_ptr<scalar_t>();

        auto cgf = DPCPP_Q_CGF(cgh) {
          auto kfn = DPCPP_Q_KFN(sycl::nd_item<1> item) {
            int64_t linearId = item.get_group(0) * item.get_local_range(0) +
                item.get_local_id(0);
            if (linearId >= input_numel)
              return;
            // multiply values at each channel with weight[channel_index]
            int64_t channel = (linearId % input_stride0) / input_stride1;
            scalar_t input_data_val = input_data[linearId];
            result_data[linearId] = (input_data_val > 0)
                ? input_data_val
                : weight_data[channel] * input_data_val;
          };
          cgh.parallel_for(
              sycl::nd_range<1>(num_groups * group_size, group_size), kfn);
        };
        DPCPP_Q_SUBMIT(sycl_queue, cgf);
      });
}

inline void launch_prelu_backward_kernel_share_weights(
    TensorIteratorBase& iter,
    const TensorBase& weight) {
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.input_dtype(),
      "prelu_backward",
      [&] {
        const auto* weight_data = weight.data_ptr<scalar_t>();
        dpcpp_kernel_multiple_outputs_for_tensor_iter(
            iter,
            [=](scalar_t input,
                scalar_t grad_out) -> std::tuple<scalar_t, bool> {
              scalar_t input_grad =
                  input > 0 ? grad_out : (*weight_data) * grad_out;
              scalar_t weight_grad_collector =
                  input > 0 ? scalar_t(0) : input * grad_out;
              return {input_grad, weight_grad_collector};
            });
      });
}

inline void launch_prelu_backward_kernel_multi_weights(
    const TensorBase& input,
    const TensorBase& weight,
    const TensorBase& grad_out,
    const TensorBase& input_grad,
    const TensorBase& weight_grad_collector) {
  auto& sycl_queue = dpcppGetCurrentQueue();
  int64_t input_ndim = input.dim();
  TORCH_CHECK(input_ndim > 0, "Not allow zero-dim input tensor.");

  int64_t channel_size = 1; // channel_size default to 1
  int64_t input_stride0 = 1, input_stride1 = 1;

  if (input_ndim > 1) {
    channel_size = input.size(1); // channel is the 2nd dim of input
    auto strides = input.strides();
    input_stride0 = strides[0];
    input_stride1 = strides[1];
  }
  const int64_t weight_num = weight.numel();
  TORCH_CHECK(
      channel_size == weight_num,
      "Mismatch of parameter numbers and input channel size. Found parameter numbers = ",
      weight_num,
      " and channel size = ",
      channel_size,
      ".");

  // config to run cuda kernel
  int64_t input_numel = input.numel();
  auto group_size = std::min(
      static_cast<int64_t>(
          dpcppMaxWorkGroupSize(dpcppGetDeviceIdOfCurrentQueue())),
      input_numel);
  auto num_groups = (input_numel + group_size - 1) / group_size;
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      input.scalar_type(),
      "prelu_backward",
      [&] {
        auto input_data = input.data_ptr<scalar_t>();
        auto weight_data = weight.data_ptr<scalar_t>();
        auto grad_out_data = grad_out.data_ptr<scalar_t>();
        auto input_grad_data = input_grad.data_ptr<scalar_t>();
        auto weight_grad_collector_data =
            weight_grad_collector.data_ptr<scalar_t>();

        auto cgf = DPCPP_Q_CGF(cgh) {
          auto kfn = DPCPP_Q_KFN(sycl::nd_item<1> item) {
            int64_t linearId = item.get_group(0) * item.get_local_range(0) +
                item.get_local_id(0);
            if (linearId >= input_numel)
              return;
            int64_t channel = (linearId % input_stride0) / input_stride1;
            scalar_t input_data_val = input_data[linearId];
            scalar_t grad_out_data_val = grad_out_data[linearId];
            input_grad_data[linearId] = (input_data_val > 0)
                ? grad_out_data_val
                : weight_data[channel] * grad_out_data_val;
            weight_grad_collector_data[linearId] = (input_data_val > 0)
                ? scalar_t(0)
                : input_data_val * grad_out_data_val;
          };
          cgh.parallel_for(
              sycl::nd_range<1>(num_groups * group_size, group_size), kfn);
        };
        DPCPP_Q_SUBMIT(sycl_queue, cgf);
      });
}

inline Tensor threshold_out(
    optional<Tensor> opt_result,
    const Tensor& self,
    const Scalar& threshold,
    const Scalar& value,
    const Tensor& other) {
  Tensor result = opt_result.value_or(Tensor());
  if (IPEX_ANY(xpu::oneDNN::is_onednn_layout, self, other) &&
      0.0 == threshold.to<float>() && 0.0 == value.to<float>() &&
      IPEX_ALL(xpu::oneDNN::eltwise_backward_valid, self, other)) {
    // need or not
    // 1. input is oneDNN layout
    // 2. it is a relu bwd (threshold and value)
    // can or not
    // 1. input is a valid memory supported by oneDNN
    xpu::oneDNN::eltwise_backward<dnnl::algorithm::eltwise_relu>(
        result, self, other, 0.0f, 0.0f);
    return result;
  } else {
    auto _self = to_plain_if_needed(self);
    auto _other = to_plain_if_needed(other);
    auto iter = TensorIterator::binary_op(result, _self, _other);
    IPEX_DISPATCH_ALL_TYPES_AND2(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        iter.dtype(),
        "threshold",
        [&] {
          scalar_t _threshold = threshold.to<scalar_t>();
          scalar_t _value = value.to<scalar_t>();
          dpcpp_kernel_for_tensor_iter(
              iter, [=](scalar_t x, scalar_t other) -> scalar_t {
                return x <= _threshold ? _value : other;
              });
        });
    return iter.output();
  }
}

template <typename scalar_t>
inline scalar_t relu_forward(scalar_t self) {
  return self > 0 ? self : static_cast<scalar_t>(0);
}

template <typename scalar_t>
inline scalar_t gelu_erf_forward(scalar_t x) {
  using opmath_t = at::opmath_type<scalar_t>;
  constexpr opmath_t kAlpha = M_SQRT1_2;
  return static_cast<opmath_t>(x) * opmath_t(0.5) *
      (opmath_t(1) + ::erf(static_cast<opmath_t>(x) * kAlpha));
}

template <typename scalar_t>
inline scalar_t gelu_tanh_forward(scalar_t x) {
  using opmath_t = at::opmath_type<scalar_t>;
  constexpr opmath_t kBeta = M_SQRT2 * M_2_SQRTPI * opmath_t(0.5);
  constexpr opmath_t kKappa = 0.044715;
  auto x_cube = static_cast<opmath_t>(x) * static_cast<opmath_t>(x) *
      static_cast<opmath_t>(x);
  auto inner = kBeta * (static_cast<opmath_t>(x) + kKappa * x_cube);
  return opmath_t(0.5) * static_cast<opmath_t>(x) *
      (opmath_t(1) + Numerics<opmath_t>::tanh(inner));
}

template <typename scalar_t>
inline scalar_t gelu_erf_backward(scalar_t dy, scalar_t x) {
  using opmath_t = at::opmath_type<scalar_t>;
  constexpr opmath_t kBeta = M_2_SQRTPI * M_SQRT1_2 * opmath_t(0.5);
  constexpr opmath_t kAlpha = M_SQRT1_2;
  const opmath_t cdf =
      opmath_t(0.5) * (opmath_t(1) + ::erf(static_cast<opmath_t>(x) * kAlpha));
  const opmath_t pdf = Numerics<opmath_t>::exp(
                           opmath_t(-0.5) * static_cast<opmath_t>(x) *
                           static_cast<opmath_t>(x)) *
      kBeta;
  return static_cast<opmath_t>(dy) * (cdf + static_cast<opmath_t>(x) * pdf);
}

template <typename scalar_t>
inline scalar_t gelu_tanh_backward(scalar_t dy, scalar_t x) {
  using opmath_t = at::opmath_type<scalar_t>;
  constexpr opmath_t kBeta = M_SQRT2 * M_2_SQRTPI * opmath_t(0.5);
  constexpr opmath_t kKappa = 0.044715;
  auto x_sq = static_cast<opmath_t>(x) * static_cast<opmath_t>(x);
  auto x_cube = x_sq * static_cast<opmath_t>(x);
  auto inner = kBeta * (static_cast<opmath_t>(x) + kKappa * x_cube);
  auto tanh_inner = Numerics<opmath_t>::tanh(inner);

  auto left = opmath_t(0.5) * static_cast<opmath_t>(x);
  auto right = opmath_t(1) + tanh_inner;

  auto left_derivative = 0.5 * right;

  auto tanh_derivative = opmath_t(1) - tanh_inner * tanh_inner;
  auto inner_derivative = kBeta * (opmath_t(1) + opmath_t(3) * kKappa * x_sq);
  auto right_derivative = left * tanh_derivative * inner_derivative;

  return static_cast<opmath_t>(dy) * (left_derivative + right_derivative);
}

Tensor& silu_out_kernel(const Tensor& self, Tensor& result) {
  auto iter = TensorIterator::unary_float_op(result, self);
  IPEX_DISPATCH_FLOATING_AND_COMPLEX_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.common_dtype(),
      "_silu_out",
      [&]() {
        result.resize_as_(self);
        dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t x) -> scalar_t {
          scalar_t one = (scalar_t)1.0;
          return x / (one + Numerics<scalar_t>::exp(-static_cast<scalar_t>(x)));
        });
      });
  return result;
}

template <typename scalar_t>
void silu_backward_kernel(
    Tensor& gradInput,
    const Tensor& gradOutput,
    const Tensor& self) {
  gradInput.resize_as_(self);
  auto iter = TensorIteratorConfig()
                  .set_check_mem_overlap(true)
                  .add_output(gradInput)
                  .add_input(gradOutput)
                  .add_input(self)
                  .build();
  dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t dy, scalar_t x) -> scalar_t {
    scalar_t one = (scalar_t)1.0;
    const scalar_t sigmoid =
        one / (one + Numerics<scalar_t>::exp(-static_cast<scalar_t>(x)));
    return dy * sigmoid * (one + x * (one - sigmoid));
  });
}

template <typename scalar_t>
inline scalar_t mish_forward(scalar_t self) {
  using T_ACC = acc_type<scalar_t>;
  const T_ACC x_acc = static_cast<T_ACC>(self);
  return (
      scalar_t)(x_acc * Numerics<T_ACC>::tanh(Numerics<T_ACC>::log1p(Numerics<T_ACC>::exp(x_acc))));
}
} // namespace impl

Tensor relu(const Tensor& self) {
  if (xpu::oneDNN::is_onednn_layout(self) &&
      xpu::oneDNN::eltwise_forward_valid(self)) {
    Tensor result;
    xpu::oneDNN::eltwise<dnnl::algorithm::eltwise_relu>(
        result, self, 0.0f, 0.0f);
    return result;
  } else {
    auto _self = to_plain_if_needed(self);
    auto result = at::empty_like(_self);
    auto iter = TensorIterator::unary_op(result, _self);
    IPEX_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        iter.dtype(),
        "relu",
        [&]() {
          dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t self) -> scalar_t {
            return impl::relu_forward<scalar_t>(self);
          });
        });
    return result;
  }
}

Tensor& relu_(Tensor& self) {
  if (xpu::oneDNN::is_onednn_layout(self) &&
      xpu::oneDNN::eltwise_forward_valid(self)) {
    xpu::oneDNN::eltwise<dnnl::algorithm::eltwise_relu>(self, self, 0.0f, 0.0f);
    return self;
  } else {
    self = to_plain_if_needed_(self);
    auto iter = TensorIterator::unary_op(self, self);
    IPEX_DISPATCH_ALL_TYPES_AND2(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        iter.dtype(),
        "relu_",
        [&]() {
          dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t self) -> scalar_t {
            return impl::relu_forward<scalar_t>(self);
          });
        });
    return self;
  }
}

Tensor& threshold_(Tensor& self, const Scalar& threshold, const Scalar& value) {
  impl::threshold_out(make_optional(self), self, threshold, value, self);
  return self;
}

Tensor threshold(
    const Tensor& self,
    const Scalar& threshold,
    const Scalar& value) {
  return impl::threshold_out(nullopt, self, threshold, value, self);
}

Tensor& threshold_out(
    const Tensor& self,
    const Scalar& threshold,
    const Scalar& value,
    Tensor& result) {
  impl::threshold_out(make_optional(result), self, threshold, value, self);
  return result;
}

Tensor threshold_backward(
    const Tensor& grad,
    const Tensor& self,
    const Scalar& threshold) {
  return impl::threshold_out(nullopt, self, threshold, 0, grad);
}

Tensor& threshold_backward_out(
    const Tensor& grad,
    const Tensor& self,
    const Scalar& threshold,
    Tensor& gradInput) {
  impl::threshold_out(make_optional(gradInput), self, threshold, 0, grad);
  return gradInput;
}

Tensor& rrelu_with_noise_out(
    const Tensor& self,
    const Tensor& noise,
    const Scalar& lower,
    const Scalar& upper,
    bool training,
    c10::optional<Generator> generator,
    Tensor& out) {
  at::native::resize_output(out, self.sizes());
  if (self.numel() == 0) {
    return out;
  }
  if (training) {
    IPEX_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::Half,
        at::ScalarType::BFloat16,
        self.scalar_type(),
        "rrelu_with_noise_out",
        [&] {
          impl::_rrelu_with_noise_train<scalar_t>(
              out, self, noise, lower, upper, generator);
        });
  } else {
    auto lower_tensor = lower.to<double>();
    auto upper_tensor = upper.to<double>();
    Scalar negative_slope = (lower_tensor + upper_tensor) / 2;
    at::leaky_relu_out(out, self, negative_slope);
  }
  return out;
}

Tensor rrelu_with_noise(
    const Tensor& self,
    const Tensor& noise,
    const Scalar& lower,
    const Scalar& upper,
    bool training,
    c10::optional<Generator> generator) {
  Tensor output = at::empty_like(self, LEGACY_CONTIGUOUS_MEMORY_FORMAT);
  return rrelu_with_noise_out(
      self, noise, lower, upper, training, generator, output);
}

Tensor& rrelu_with_noise_(
    Tensor& self,
    const Tensor& noise,
    const Scalar& lower,
    const Scalar& upper,
    bool training,
    c10::optional<Generator> generator) {
  return rrelu_with_noise_out(
      self, noise, lower, upper, training, generator, self);
}

Tensor rrelu_with_noise_backward(
    const Tensor& grad_output,
    const Tensor& self,
    const Tensor& noise,
    const Scalar& lower,
    const Scalar& upper,
    bool training,
    bool self_is_result) {
  if (training) {
    return noise * grad_output;
  } else {
    auto l = lower.toDouble();
    auto u = upper.toDouble();
    auto mid = (l + u) / 2.;
    return at::leaky_relu_backward(grad_output, self, mid, self_is_result);
  }
}

Tensor prelu(const Tensor& self, const Tensor& weight_) {
  auto input = self.contiguous();
  auto weight = weight_.contiguous();

  TORCH_CHECK(input.is_contiguous());
  TORCH_CHECK(weight.is_contiguous());

  int64_t weight_num = weight.numel();
  int64_t weight_dim = weight.dim();
  Tensor result = at::empty_like(input, LEGACY_CONTIGUOUS_MEMORY_FORMAT);

  TORCH_CHECK(
      weight_dim == 0 || weight_dim == 1,
      "prelu: Expected `weight` to be a scalar or 1D tensor, but got ndim = ",
      weight_dim);

  // case1: shared weight for all channels
  if (weight_num == 1) {
    auto iter = TensorIterator::unary_op(result, input);
    impl::launch_prelu_kernel_share_weights(iter, weight);
  } else { // case2: multiple weights, one for each channel
    impl::launch_prelu_kernel_multi_weights(result, input, weight);
  }
  return result;
}

std::tuple<Tensor, Tensor> prelu_backward(
    const Tensor& grad_out_,
    const Tensor& self,
    const Tensor& weight_) {
  auto input = self.contiguous();
  auto grad_out = grad_out_.contiguous();
  auto weight = weight_.contiguous();

  TORCH_CHECK(input.is_contiguous());
  TORCH_CHECK(grad_out.is_contiguous());
  TORCH_CHECK(weight.is_contiguous());

  int64_t weight_num = weight.numel();
  auto dims = input.dim();
  Tensor input_grad = at::empty_like(input, LEGACY_CONTIGUOUS_MEMORY_FORMAT);
  Tensor weight_grad = at::empty_like(weight, LEGACY_CONTIGUOUS_MEMORY_FORMAT);
  Tensor weight_grad_collector =
      at::empty_like(input, LEGACY_CONTIGUOUS_MEMORY_FORMAT);

  // case1: shared parameter for all channels
  if (weight_num == 1) {
    at::TensorIterator iter = TensorIteratorConfig()
                                  .add_output(input_grad)
                                  .add_output(weight_grad_collector)
                                  .add_input(input)
                                  .add_input(grad_out)
                                  .build();

    impl::launch_prelu_backward_kernel_share_weights(iter, weight);
    weight_grad.fill_(weight_grad_collector.sum());
  } else { // case2: multiple parameters, one for each channel
    impl::launch_prelu_backward_kernel_multi_weights(
        input, weight, grad_out, input_grad, weight_grad_collector);
    // update weight_grad
    std::vector<int64_t> reduce_dims;
    reduce_dims.push_back(0);
    if (dims > 2) {
      for (int64_t i = 2; i < dims; i++)
        reduce_dims.push_back(i);
    }
    weight_grad = weight_grad_collector.sum(reduce_dims);
  }
  return std::tuple<Tensor, Tensor>{input_grad, weight_grad};
}

Tensor& hardshrink_out(
    const Tensor& self,
    const Scalar& lambd,
    Tensor& result) {
  auto iter = TensorIterator::unary_op(result, self);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "hardshrink",
      [&]() {
        auto _lambd = lambd.to<scalar_t>();
        dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t x) -> scalar_t {
          return (x >= -_lambd && x <= _lambd) ? scalar_t(0) : x;
        });
      });
  return result;
}

Tensor hardshrink(const Tensor& self, const Scalar& lambd) {
  Tensor result = at::empty_like(self);
  return hardshrink_out(self, lambd, result);
}

Tensor& hardshrink_backward_out(
    const Tensor& grad,
    const Tensor& self,
    const Scalar& lambd,
    Tensor& grad_input) {
  auto iter = TensorIterator::binary_op(grad_input, grad, self);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "hardshrink_backward_out",
      [&]() {
        auto _lambd = lambd.to<scalar_t>();
        dpcpp_kernel_for_tensor_iter(
            iter, [=](scalar_t grad_output, scalar_t x) -> scalar_t {
              return (x >= -_lambd && x <= _lambd) ? scalar_t(0) : grad_output;
            });
      });
  return grad_input;
}

Tensor hardshrink_backward(
    const Tensor& grad,
    const Tensor& self,
    const Scalar& lambd) {
  auto result = at::empty_like(grad);
  return hardshrink_backward_out(grad, self, lambd, result);
}

Tensor& hardswish_out(const Tensor& self, Tensor& result) {
  auto iter = TensorIterator::unary_op(result, self);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "hardswish",
      [&]() {
        using accscalar_t = acc_type<scalar_t>;
        const accscalar_t zero(0.0f);
        const accscalar_t one_sixth(1.0f / 6.0f);
        const accscalar_t three(3.0f);
        const accscalar_t six(6.0f);
        dpcpp_kernel_for_tensor_iter(
            iter, [zero, one_sixth, three, six](scalar_t self_val) -> scalar_t {
              accscalar_t x = static_cast<accscalar_t>(self_val);
              return x *
                  Numerics<accscalar_t>::min(
                         Numerics<accscalar_t>::max(x + three, zero), six) *
                  one_sixth;
            });
      });
  return result;
}

Tensor hardswish(const Tensor& self) {
  Tensor result = at::empty_like(self);
  return at::AtenIpexTypeXPU::hardswish_out(self, result);
}

Tensor& hardswish_(Tensor& self) {
  return at::AtenIpexTypeXPU::hardswish_out(self, self);
}

Tensor hardswish_backward(const Tensor& grad_output, const Tensor& self) {
  auto result = at::empty_like(grad_output);
  auto iter = TensorIterator::binary_op(result, grad_output, self);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      iter.dtype(),
      "hardswish_backward",
      [&]() {
        using accscalar_t = acc_type<scalar_t>;
        const accscalar_t zero(0.0f);
        const accscalar_t three(3.0f);
        const accscalar_t neg_three(-3.0f);
        const accscalar_t one_half(0.5f);
        dpcpp_kernel_for_tensor_iter(
            iter,
            [zero, three, neg_three, one_half](
                scalar_t grad_val_, scalar_t self_val_) -> scalar_t {
              accscalar_t grad_val = static_cast<accscalar_t>(grad_val_);
              accscalar_t self_val = static_cast<accscalar_t>(self_val_);
              if (self_val < neg_three) {
                return zero;
              } else if (self_val <= three) {
                return grad_val * ((self_val / three) + one_half);
              } else {
                return grad_val;
              }
            });
      });
  return result;
}

Tensor& gelu_out(
    const Tensor& self,
    c10::string_view approximate,
    Tensor& result) {
  auto _approximate = at::native::get_gelutype_enum(approximate);
  if (xpu::oneDNN::is_onednn_layout(self) &&
      xpu::oneDNN::eltwise_forward_valid(self)) {
    if (_approximate == at::native::GeluType::Tanh) {
      xpu::oneDNN::eltwise<dnnl::algorithm::eltwise_gelu_tanh>(
          result, self, 0.0f, 0.0f);
    } else {
      xpu::oneDNN::eltwise<dnnl::algorithm::eltwise_gelu_erf>(
          result, self, 0.0f, 0.0f);
    }
    return result;
  } else {
    auto _self = to_plain_if_needed(self);
    if (!result.defined()) {
      result = at::empty_like(_self);
    }
    auto iter = TensorIterator::unary_op(result, _self);

    if (_approximate == at::native::GeluType::Tanh) {
      IPEX_DISPATCH_FLOATING_TYPES_AND2(
          at::ScalarType::BFloat16,
          at::ScalarType::Half,
          iter.dtype(),
          "gelu",
          [&]() {
            dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t self) -> scalar_t {
              return impl::gelu_tanh_forward<scalar_t>(self);
            });
          });
    } else {
      IPEX_DISPATCH_FLOATING_TYPES_AND2(
          at::ScalarType::BFloat16,
          at::ScalarType::Half,
          iter.dtype(),
          "gelu",
          [&]() {
            dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t self) -> scalar_t {
              return impl::gelu_erf_forward<scalar_t>(self);
            });
          });
    }
    return result;
  }
}

Tensor gelu(const Tensor& self, c10::string_view approximate) {
  Tensor result;
  return gelu_out(self, approximate, result);
}

Tensor& gelu_backward_out(
    const Tensor& grad,
    const Tensor& self,
    c10::string_view approximate,
    Tensor& grad_input) {
  auto _approximate = at::native::get_gelutype_enum(approximate);
  if (IPEX_ANY(xpu::oneDNN::is_onednn_layout, grad, self) &&
      IPEX_ALL(xpu::oneDNN::eltwise_backward_valid, grad, self)) {
    if (_approximate == at::native::GeluType::Tanh) {
      xpu::oneDNN::eltwise_backward<dnnl::algorithm::eltwise_gelu_tanh>(
          grad_input, self, grad, 0.0f, 0.0f);
    } else {
      xpu::oneDNN::eltwise_backward<dnnl::algorithm::eltwise_gelu_erf>(
          grad_input, self, grad, 0.0f, 0.0f);
    }
    return grad_input;
  } else {
    auto _self = to_plain_if_needed(self);
    auto _grad = to_plain_if_needed(grad);
    if (!grad_input.defined()) {
      grad_input = at::empty_like(_self);
    }
    auto iter = TensorIterator::binary_op(grad_input, _grad, _self);
    if (_approximate == at::native::GeluType::Tanh) {
      IPEX_DISPATCH_FLOATING_TYPES_AND2(
          at::ScalarType::BFloat16,
          at::ScalarType::Half,
          iter.dtype(),
          "gelu_backward",
          [&]() {
            dpcpp_kernel_with_scalars(
                iter, [=](scalar_t grad, scalar_t self) -> scalar_t {
                  return impl::gelu_tanh_backward<scalar_t>(grad, self);
                });
          });
    } else {
      IPEX_DISPATCH_FLOATING_TYPES_AND2(
          at::ScalarType::BFloat16,
          at::ScalarType::Half,
          iter.dtype(),
          "gelu_backward",
          [&]() {
            dpcpp_kernel_with_scalars(
                iter, [=](scalar_t grad, scalar_t self) -> scalar_t {
                  return impl::gelu_erf_backward<scalar_t>(grad, self);
                });
          });
    }
    return grad_input;
  }
}

Tensor gelu_backward(
    const Tensor& grad,
    const Tensor& self,
    c10::string_view approximate) {
  Tensor result;
  return gelu_backward_out(grad, self, approximate, result);
}

Tensor& silu_out(const Tensor& self, Tensor& output) {
  return impl::silu_out_kernel(self, output);
}

Tensor& silu_backward_out(
    const Tensor& grad_output,
    const Tensor& output,
    Tensor& grad_input) {
  IPEX_DISPATCH_FLOATING_TYPES_AND(
      at::ScalarType::BFloat16,
      output.scalar_type(),
      "silu_backward_out",
      [&]() {
        impl::silu_backward_kernel<scalar_t>(grad_input, grad_output, output);
      });
  return grad_input;
}

at::Tensor& mish_out(const at::Tensor& self, at::Tensor& out) {
  if (xpu::oneDNN::is_onednn_layout(self) &&
      xpu::oneDNN::eltwise_forward_valid(self)) {
    xpu::oneDNN::eltwise<dnnl::algorithm::eltwise_mish>(out, self, 0.0f, 0.0f);
    return out;
  } else {
    auto _self = to_plain_if_needed(self);
    if (!out.defined()) {
      out = at::empty_like(_self);
    }
    auto iter = TensorIterator::unary_op(out, _self);
    IPEX_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        iter.dtype(),
        "mish",
        [&]() {
          dpcpp_kernel_for_tensor_iter(iter, [=](scalar_t self) -> scalar_t {
            return impl::mish_forward<scalar_t>(self);
          });
        });
    return out;
  }
}

} // namespace AtenIpexTypeXPU
} // namespace at
