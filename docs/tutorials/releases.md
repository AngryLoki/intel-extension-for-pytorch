Releases
=============

## 1.13.10+xpu

Intel® Extension for PyTorch\* v1.13.10+xpu extends PyTorch\* 1.13 with up-to-date features and optimizations on `xpu` for an extra performance boost on Intel hardware. Optimizations take advantage of AVX-512 Vector Neural Network Instructions (AVX512 VNNI) and Intel® Advanced Matrix Extensions (Intel® AMX) on Intel CPUs as well as Intel Xe Matrix Extensions (XMX) AI engines on Intel discrete GPUs. Moreover, through PyTorch* `xpu` device, Intel® Extension for PyTorch* provides easy GPU acceleration for Intel discrete GPUs with PyTorch*.

### Highlights

This release introduces specific XPU solution optimizations on Intel discrete GPUs which include Intel® Data Center GPU Flex Series 170 and Intel® Data Center GPU Max Series. Optimized operators and kernels are implemented and registered through PyTorch\* dispatching mechanism for the `xpu` device. These operators and kernels are accelerated on Intel GPU hardware from the corresponding native vectorization and matrix calculation features. In graph mode, additional operator fusions are supported to reduce operator/kernel invocation overheads, and thus increase performance.

This release provides the following features:
- Usability and Performance Features listed in [Intel® Extension for PyTorch\* v1.13.0+cpu release](https://intel.github.io/intel-extension-for-pytorch/cpu/1.13.0+cpu/tutorials/releases.html#id1)
- Distributed Training
  - support of distributed training with DistributedDataParallel (DDP) on Intel GPU hardware
  - support of distributed training with Horovod (experimental) on Intel GPU hardware
- DLPack Solution
  - mechanism to share tensor data without copy when interoparate with other libraries on Intel GPU hardware
- Legacy Profiler Tool
  - an extension of PyTorch* legacy profiler for profiling operators' overhead on Intel GPU hardware
- Simple Trace Tool
  - built-in debugging tool to print out the call stack for a piece of code

This release adds the following fusion patterns in PyTorch\* JIT mode for Intel GPU:
- `Conv2D` + UnaryOp(`abs`, `sqrt`, `square`, `exp`, `log`, `round`, `GeLU`, `Log_Sigmoid`, `Hardswish`, `Mish`, `HardSigmoid`, `Tanh`, `Pow`, `ELU`, `hardtanh`)
- `Linear` + UnaryOp(`abs`, `sqrt`, `square`, `exp`, `log`, `round`, `Log_Sigmoid`, `Hardswish`, `HardSigmoid`, `Pow`, `ELU`, `SiLU`, `hardtanh`, `Leaky_relu`)
### Known Issues

Please refer to [Known Issues webpage](./performance_tuning/known_issues.md).

## 1.10.200+gpu

Intel® Extension for PyTorch\* v1.10.200+gpu extends PyTorch\* 1.10 with up-to-date features and optimizations on XPU for an extra performance boost on Intel Graphics cards. XPU is a user visible device that is a counterpart of the well-known CPU and CUDA in the PyTorch\* community. XPU represents an Intel-specific kernel and graph optimizations for various “concrete” devices. The XPU runtime will choose the actual device when executing AI workloads on the XPU device. The default selected device is Intel GPU. XPU kernels from Intel® Extension for PyTorch\* are written in [DPC++](https://github.com/intel/llvm#oneapi-dpc-compiler) that supports [SYCL language](https://registry.khronos.org/SYCL/specs/sycl-2020/html/sycl-2020.html) and also a number of [DPC++ extensions](https://github.com/intel/llvm/tree/sycl/sycl/doc/extensions).

### Highlights

This release introduces specific XPU solution optimizations on Intel® Data Center GPU Flex Series 170. Optimized operators and kernels are implemented and registered through PyTorch\* dispatching mechanism for the XPU device. These operators and kernels are accelerated on Intel GPU hardware from the corresponding native vectorization and matrix calculation features. In graph mode, additional operator fusions are supported to reduce operator/kernel invocation overheads, and thus increase performance.

This release provides the following features:
- Auto Mixed Precision (AMP)
  - support of AMP with BFloat16 and Float16 optimization of GPU operators
- Channels Last
  - support of channels\_last (NHWC) memory format for most key GPU operators
- DPC++ Extension
  - mechanism to create PyTorch\* operators with custom DPC++ kernels running on the XPU device
- Optimized Fusion
  - support of SGD/AdamW fusion for both FP32 and BF16 precision

This release supports the following fusion patterns in PyTorch\* JIT mode:

- Conv2D + ReLU
- Conv2D + Sum
- Conv2D + Sum + ReLU
- Pad + Conv2d
- Conv2D + SiLu
- Permute + Contiguous
- Conv3D + ReLU
- Conv3D + Sum
- Conv3D + Sum + ReLU
- Linear + ReLU
- Linear + Sigmoid
- Linear + Div(scalar)
- Linear + GeLu
- Linear + GeLu\_
- T + Addmm
- T + Addmm + ReLu
- T + Addmm + Sigmoid
- T + Addmm + Dropout
- T + Matmul
- T + Matmul + Add
- T + Matmul + Add + GeLu
- T + Matmul + Add + Dropout
- Transpose + Matmul
- Transpose + Matmul + Div
- Transpose + Matmul + Div + Add
- MatMul + Add
- MatMul + Div
- Dequantize + PixelShuffle
- Dequantize + PixelShuffle + Quantize
- Mul + Add
- Add + ReLU
- Conv2D + Leaky\_relu
- Conv2D + Leaky\_relu\_
- Conv2D + Sigmoid
- Conv2D + Dequantize
- Softplus + Tanh
- Softplus + Tanh + Mul
- Conv2D + Dequantize + Softplus + Tanh + Mul
- Conv2D + Dequantize + Softplus + Tanh + Mul + Quantize
- Conv2D + Dequantize + Softplus + Tanh + Mul + Quantize + Add

### Known Issues

- [CRITICAL ERROR] Kernel 'XXX' removed due to usage of FP64 instructions unsupported by the targeted hardware

    FP64 is not natively supported by the [Intel® Data Center GPU Flex Series](https://www.intel.com/content/www/us/en/products/docs/discrete-gpus/data-center-gpu/flex-series/overview.html) platform. If you run any AI workload on that platform and receive this error message, it means a kernel requiring FP64 instructions is removed and not executed, hence the accuracy of the whole workload is wrong.

- symbol undefined caused by \_GLIBCXX\_USE\_CXX11\_ABI

    ```bash
    ImportError: undefined symbol: _ZNK5torch8autograd4Node4nameB5cxx11Ev
    ```
    
    DPC++ does not support \_GLIBCXX\_USE\_CXX11\_ABI=0, Intel® Extension for PyTorch\* is always compiled with \_GLIBCXX\_USE\_CXX11\_ABI=1. This symbol undefined issue appears when PyTorch\* is compiled with \_GLIBCXX\_USE\_CXX11\_ABI=0. Update PyTorch\* CMAKE file to set \_GLIBCXX\_USE\_CXX11\_ABI=1 and compile PyTorch\* with particular compiler which supports \_GLIBCXX\_USE\_CXX11\_ABI=1. We recommend to use gcc version 9.4.0 on ubuntu 20.04.

- Can't find oneMKL library when build Intel® Extension for PyTorch\* without oneMKL

    ```bash
    /usr/bin/ld: cannot find -lmkl_sycl
    /usr/bin/ld: cannot find -lmkl_intel_ilp64
    /usr/bin/ld: cannot find -lmkl_core
    /usr/bin/ld: cannot find -lmkl_tbb_thread
    dpcpp: error: linker command failed with exit code 1 (use -v to see invocation)
    ```
    
    When PyTorch\* is built with oneMKL library and Intel® Extension for PyTorch\* is built without oneMKL library, this linker issue may occur. Resolve it by setting:
    
    ```bash
    export USE_ONEMKL=OFF
    export MKL_DPCPP_ROOT=${PATH_To_Your_oneMKL}/__release_lnx/mkl
    ```
    
    Then clean build Intel® Extension for PyTorch\*.

- undefined symbol: mkl\_lapack\_dspevd. Intel MKL FATAL ERROR: cannot load libmkl\_vml\_avx512.so.2 or libmkl\_vml\_def.so.2

    This issue may occur when Intel® Extension for PyTorch\* is built with oneMKL library and PyTorch\* is not build with any MKL library. The oneMKL kernel may run into CPU backend incorrectly and trigger this issue. Resolve it by installing MKL library from conda:
    
    ```bash
    conda install mkl
    conda install mkl-include
    ```
    
    then clean build PyTorch\*.

- OSError: libmkl\_intel\_lp64.so.1: cannot open shared object file: No such file or directory

    Wrong MKL library is used when multiple MKL libraries exist in system. Preload oneMKL by:
    
    ```bash
    export LD_PRELOAD=${MKL_DPCPP_ROOT}/lib/intel64/libmkl_intel_lp64.so.1:${MKL_DPCPP_ROOT}/lib/intel64/libmkl_intel_ilp64.so.1:${MKL_DPCPP_ROOT}/lib/intel64/libmkl_sequential.so.1:${MKL_DPCPP_ROOT}/lib/intel64/libmkl_core.so.1:${MKL_DPCPP_ROOT}/lib/intel64/libmkl_sycl.so.1
    ```
    
    If you continue seeing similar issues for other shared object files, add the corresponding files under ${MKL\_DPCPP\_ROOT}/lib/intel64/ by `LD_PRELOAD`. Note that the suffix of the libraries may change (e.g. from .1 to .2), if more than one oneMKL library is installed on the system.

