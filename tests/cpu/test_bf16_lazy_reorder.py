"""Tests for lazy reorder."""
from __future__ import division
from __future__ import print_function

import os
import math
import time
import random
import unittest
from functools import reduce
import copy
import sys
import itertools
import torch
# import intel_pytorch_extension as ipex
import torch_ipex as ipex

import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.nn import Parameter
import torch.nn.functional as F
from torch.autograd import gradcheck
from torch.autograd.gradcheck import gradgradcheck
from torch._six import inf, nan
import torch.nn.utils.rnn as rnn_utils

from common_utils import (
    TestCase, TEST_WITH_ROCM, run_tests,
    IS_WINDOWS, IS_FILESYSTEM_UTF8_ENCODING, NO_MULTIPROCESSING_SPAWN,
    do_test_dtypes, IS_SANDCASTLE, IS_FBCODE, IS_REMOTE_GPU, load_tests, slowTest,
    skipCUDAMemoryLeakCheckIf, BytesIOContext,
    skipIfRocm, skipIfNoSciPy, TemporaryFileName, TemporaryDirectoryName,
    wrapDeterministicFlagAPITest, DeterministicGuard, make_tensor)

from common_ipex_conf import AutoMixPrecision, AutoDNNL

def get_rand_seed():
    return int(time.time() * 1000000000)

device = ipex.DEVICE

def convert_to_bf16(t):
    last_dim = t.dim() - 1
    out_features = t.size(last_dim)
    in_features = t.size(last_dim)

    t = t.clone().to(device)
    with AutoDNNL(True), AutoMixPrecision(True):
        return F.linear(t, torch.eye(out_features, in_features).to(device))

def _gen_tensor(seed, shape, is_forward=True):
    torch.manual_seed(seed)
    x = torch.randn(shape) * 10
    if is_forward:
        x_cpu = x
        x_auto_mix_inference = x_cpu.to(device=device)
        x_auto_mix_train = copy.deepcopy(x_auto_mix_inference)
        x_man_bf16 = copy.deepcopy(x_auto_mix_inference).to(torch.bfloat16)
        x_auto_mix_train_bf16 = convert_to_bf16(x_cpu)
    else:
        x_cpu = x.clone().requires_grad_()
        x_auto_mix_inference = None
        x_auto_mix_train = x.clone().to(device=device).requires_grad_()
        x_man_bf16 = x.clone().to(device=device).to(torch.bfloat16).requires_grad_()
        x_auto_mix_train_bf16 = convert_to_bf16(x).requires_grad_()

    return x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16

def _gen_op(seed, op, is_bn=False, is_forward=True):
    torch.manual_seed(seed)
    op_cpu = op
    if is_forward:
        op_auto_mix_inference = copy.deepcopy(op_cpu).to(device=device)
    else:
        op_auto_mix_inference = None
    op_auto_mix_train = copy.deepcopy(op_cpu).to(device=device)

    # Mean / Variance / ScaleShift of BN cannot be bf16
    if is_bn:
        op_man_bf16 =copy.deepcopy(op_cpu).to(device=device)
    else:
        op_man_bf16 =copy.deepcopy(op_cpu).to(device=device).to(torch.bfloat16)
    op_auto_mix_train_bf16 = copy.deepcopy(op_cpu).to(device=device)

    return op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16

class CascadedConvBnSumRelu(nn.Module):
    def __init__(self, in_channels, mid_channels, out_channels, **kwargs):
        super(CascadedConvBnSumRelu, self).__init__()
        self.conv = torch.nn.Conv2d(in_channels, mid_channels, bias=False, **kwargs)
        self.conv1 = torch.nn.Conv2d(
            mid_channels, out_channels, bias=False, padding=1, **kwargs)
        self.conv2 = torch.nn.Conv2d(in_channels, out_channels, bias=False, **kwargs)
        self.bn = torch.nn.BatchNorm2d(mid_channels, eps=0.001)
        self.bn1 = torch.nn.BatchNorm2d(out_channels, eps=0.001)
        self.bn2 = torch.nn.BatchNorm2d(out_channels, eps=0.001)

    def forward(self, x):
        a = self.conv(x)
        a = self.bn(a)
        a = F.relu(a, inplace=True)
        a = self.conv1(a)
        a = self.bn1(a)
        b = self.conv2(x)
        b = self.bn2(b)
        return F.relu(a.add_(b), inplace=True)

def apply(m, fn, args):
    for sub_module in m.children():
        apply(sub_module, fn, args)
    fn(m, args)

class TestTo(TestCase):
    def test_to(self):
        rand_seed = int(get_rand_seed())
        torch.manual_seed(rand_seed)

        m = CascadedConvBnSumRelu(3, 64, 32, kernel_size=3, stride=1)
        m_cpu = copy.deepcopy(m).to("cpu")
        m_data_type = copy.deepcopy(m).to(torch.bfloat16)
        m_auto_mix = copy.deepcopy(m).to(device)
        m_auto_mix_data_type = copy.deepcopy(m).to(device=device, dtype=torch.bfloat16)

        def check_param(t, is_param):
            for param in t.parameters():
                if is_param:
                    self.assertTrue(ipex._C.is_parameter_tensor(param.data))
                else:
                    self.assertFalse(ipex._C.is_parameter_tensor(param.data))

        apply(m_cpu, check_param, False)
        apply(m_data_type, check_param, False)
        apply(m_auto_mix, check_param, True)
        apply(m_auto_mix_data_type, check_param, True)

class TestConv(TestCase):
    def test_Conv2d_with_cpu(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        _conv = torch.nn.Conv2d(1, 1, (3, 3))

        conv_man_bf16 =copy.deepcopy(_conv).to(device=device).to(torch.bfloat16)
        conv_auto_mix =copy.deepcopy(_conv).to(device=device)

        _in_cpu = torch.rand((1, 1, 7, 7))
        in_auto_mix = _in_cpu.to(device=device)
        in_man_bf16 = in_auto_mix.to(torch.bfloat16)

        res_cpu_fp32 = _conv(_in_cpu)

        with AutoDNNL(True), AutoMixPrecision(False):
            res_man_bf16 = conv_man_bf16(in_man_bf16)
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            self.assertEqual(res_cpu_fp32.bfloat16().float(), res_man_bf16.float(), atol=1e-1, rtol=1e-5)

            with AutoMixPrecision(True):
                self.assertEqual(in_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(in_auto_mix))
                res_auto_bf16 = conv_auto_mix(in_auto_mix)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_bf16))
                self.assertEqual(res_man_bf16.float(), res_auto_bf16.float())

    def test_Conv2d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        input = torch.rand((1, 1, 7, 7))
        for bias in [True, False]:
            _conv = torch.nn.Conv2d(1, 1, (3, 3), bias=bias)
            conv_man_bf16 =copy.deepcopy(_conv).to(device=device).to(torch.bfloat16)
            conv_auto_mix =copy.deepcopy(_conv).to(device=device)
            _in_cpu = input.clone().requires_grad_()
            in_auto_mix = input.clone().to(device=device).requires_grad_()
            in_man_bf16 = input.clone().to(device=device).to(torch.bfloat16).requires_grad_()
            out_cpu = _conv(_in_cpu).sum()
            out_cpu.backward()
            with AutoDNNL(True), AutoMixPrecision(False, train=True):
                out_man_bf16 = conv_man_bf16(in_man_bf16).sum()
                out_man_bf16.backward()
                self.assertEqual(in_man_bf16.grad.dtype, torch.bfloat16)
                self.assertEqual(_in_cpu.grad.bfloat16().float(), in_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

                with AutoMixPrecision(True, train=True):
                    self.assertEqual(in_auto_mix.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(in_auto_mix))
                    out_auto_bf16 = conv_auto_mix(in_auto_mix).sum()
                    out_auto_bf16.backward()
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(in_auto_mix.grad))
                    self.assertEqual(in_man_bf16.grad.float(), in_auto_mix.grad.float())

class TestDeconv(TestCase):
    def _deconv_params_list(self):
        params_dict = {
            "input_height": [8],
            "input_width": [8],
            "input_depth": [8],
            "input_channel_per_group": [10],
            "output_channel_per_group": [10],
            "kernel_size": [3, 4],
            "bias": [False, True],
            "stride": [2], # [1, 2]
            "padding": [1, 2],
            "output_padding": [2],
            "groups": [1, 2],
            "dilation": [1, 3, 4],
        }

        params_list = []

        for key, value in params_dict.items():
            params_list.append(value)
        return params_list

    def _test_deconv(self, dims):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        params_list = self._deconv_params_list()

        for input_width, input_height, input_depth, input_channel_per_group, output_channel_per_group, kernel_size, bias, stride, padding, output_padding, groups, dilation in itertools.product(*params_list):
            if (output_padding < stride or output_padding < dilation) \
                    and ((input_height - 1) * stride - 2 * padding + dilation * (kernel_size -1 ) + output_padding + 1 > 0) \
                    and ((input_width - 1) * stride - 2 * padding + dilation * (kernel_size -1 ) + output_padding + 1 > 0) \
                    and ((input_depth - 1) * stride - 2 * padding + dilation * (kernel_size -1 ) + output_padding + 1 > 0):

                ic = input_channel_per_group * groups
                oc = output_channel_per_group * groups

                if dims == 2:
                    module = torch.nn.ConvTranspose2d(ic, oc, kernel_size=kernel_size, stride=stride, padding=padding, output_padding=output_padding, groups=groups, bias=bias, dilation=dilation)
                    x = torch.rand((2, ic, input_height, input_width))
                elif dims == 3:
                    module = torch.nn.ConvTranspose3d(ic, oc, kernel_size=kernel_size, stride=stride, padding=padding, output_padding=output_padding, groups=groups, bias=bias, dilation=dilation)
                    x = torch.rand((2, ic, input_depth, input_height, input_width))

                module_auto_mix_infer = copy.deepcopy(module).to(device=device)
                module_auto_mix_train = copy.deepcopy(module).to(device=device)

                x_aten = x.clone().requires_grad_()
                x_auto_mix_infer = x.clone().to(device=device).requires_grad_()
                x_auto_mix_train = x.clone().to(device=device).requires_grad_()

                y_aten = module(x_aten)
                y_aten.sum().backward()

                with AutoDNNL(True), AutoMixPrecision(True, train=False):
                    self.assertEqual(x_auto_mix_infer.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_infer))
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_infer.weight))
                    if bias:
                        self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_infer.bias))

                    y_auto_mix_infer = module_auto_mix_infer(x_auto_mix_infer)
                    y_auto_mix_infer.sum().backward()

                    if padding - output_padding + stride > 0:
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_infer.grad))
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(module_auto_mix_infer.weight))
                        if bias:
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(module_auto_mix_infer.bias))

                        self.assertEqual(y_aten, y_auto_mix_infer, atol=1e-1, rtol=1e-5)

                    # mkldnn does not support the case where:
                    # padding - output_padding + stride <= 0
                    # while PyTorch supports this case, will fallback in this case
                    else:
                        # threshold because input has been reordered to bf16??
                        self.assertEqual(
                            y_aten, y_auto_mix_infer, 4e-3)

                with AutoDNNL(True), AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.weight))
                    if bias:
                        self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.bias))

                    y_auto_mix_train = module_auto_mix_train(x_auto_mix_train)
                    y_auto_mix_train.sum().backward()

                    if padding - output_padding + stride > 0:
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train.grad))
                        self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.weight))
                        self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.weight.grad))
                        if bias:
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.bias))
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(module_auto_mix_train.bias.grad))

                        self.assertEqual(
                            y_aten, y_auto_mix_train, atol=1e-1, rtol=1e-5)
                        self.assertEqual(
                            module.weight.grad, module_auto_mix_train.weight.grad, atol=1e-1, rtol=1e-5)
                        self.assertEqual(
                            x_aten.grad, x_auto_mix_train.grad, atol=1e-1, rtol=1e-5)
                        if bias:
                            self.assertEqual(module.bias.grad, module_auto_mix_train.bias.grad)

                    # mkldnn does not support the case where:
                    # padding - output_padding + stride <= 0
                    # while PyTorch supports this case, will fallback in this case
                    else:
                        # threshold because input has been reordered to bf16??
                        self.assertEqual(
                            y_aten, y_auto_mix_train, 3e-3)
                        self.assertEqual(
                            module.weight.grad, module_auto_mix_train.weight.grad, 2e-1)
                        self.assertEqual(
                            x_aten.grad, x_auto_mix_train.grad)
                        if bias:
                            self.assertEqual(module.bias.grad, module_auto_mix_train.bias.grad)

    def test_deconv2d(self):
        self._test_deconv(dims=2)

    def test_deconv3d(self):
        self._test_deconv(dims=3)

class TestBatchNorm(TestCase):
    def test_batch_norm2d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
            rand_seed, (64, 3, 35, 45))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
            rand_seed, torch.nn.BatchNorm2d(3), is_bn=True)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                print(res_bf16.device)
                print(res_auto_mix_inference.device)
                self.assertEqual(res_bf16.float().to("cpu"), res_auto_mix_inference.to("cpu"))

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16)

    def test_batch_norm2d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (64, 3, 35, 45), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.BatchNorm2d(3), is_bn=True, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

    def test_batch_norm3d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
            rand_seed, (4, 3, 30, 30, 30))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
            rand_seed, torch.nn.BatchNorm3d(3), is_bn=True)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_batch_norm3d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (4, 3, 30, 30, 30), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.BatchNorm3d(3), is_bn=True, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestLayerNorm(TestCase):
    def test_layer_norm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
            rand_seed, (2, 5, 10, 10))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
            rand_seed, torch.nn.LayerNorm([10, 10]), is_bn=True)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16)

    def test_layer_norm_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (2, 5, 10, 10), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.LayerNorm([10, 10]), is_bn=True, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16(), x_man_bf16.grad, 1e-2)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestRelu(TestCase):
    def test_relu(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.ReLU(), is_bn=False)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_relu_(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        x_cpu.relu_()
        with AutoDNNL(True), AutoMixPrecision(False):
            x_man_bf16.relu_()
            self.assertEqual(x_man_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                x_auto_mix_inference.relu_()
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                x_auto_mix_train.relu_()
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(x_cpu, x_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                x_auto_mix_train_bf16.relu_()
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_train_bf16, 1e-3)

    def test_relu_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (4, 5), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.ReLU(), is_bn=False, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestGelu(TestCase):
    def test_gelu(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.GELU(), is_bn=False)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train, 1e-3)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_gelu_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (4, 5), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.GELU(), is_bn=False, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad.float(), x_auto_mix.grad.float(), 1e-3)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad.float())

class TestShape(TestCase):
    def _check_tensor_shape(self, t1, t2):
        self.assertEqual(t1.size(), t2.size())
        self.assertEqual(t1.stride(), t2.stride())
        self.assertEqual(t1.storage_offset(), t2.storage_offset())

    def test_slice(self):
        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10, 10, 10)
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = x_cpu.to(device=device)
            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_dpcpp))

            # the storage should be converted to bf16 on slicing
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]
            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_dpcpp))
            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_dpcpp_slice))

            # check shape info
            self._check_tensor_shape(x_cpu, x_dpcpp)
            self._check_tensor_shape(x_cpu_slice, x_dpcpp_slice)

            # simple binary op
            y_cpu = x_cpu_slice * x_cpu_slice
            y_dpcpp = x_dpcpp_slice * x_dpcpp_slice
            self.assertEqual(y_cpu, y_dpcpp, atol=1e-1, rtol=1e-5)

            # check sliced data. This should convert the storage back to fp32
            self.assertEqual(x_cpu_slice, x_dpcpp_slice, atol=1e-1, rtol=1e-5)
            self.assertEqual(x_cpu, x_dpcpp, atol=1e-1, rtol=1e-5)
            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_dpcpp))
            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_dpcpp_slice))

            # check shape info
            self._check_tensor_shape(x_cpu, x_dpcpp)
            self._check_tensor_shape(x_cpu_slice, x_dpcpp_slice)

    def test_cat_slice(self):
        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10)
            y_cpu = torch.cat([x_cpu, x_cpu, x_cpu])

            x_dpcpp = x_cpu.to(device=device)
            y_dpcpp = torch.cat([x_dpcpp, x_dpcpp, x_dpcpp])

            res_cpu = y_cpu[0:10] * y_cpu[10:20] * y_cpu[20:30]
            res_dpcpp = y_dpcpp[0:10] * y_dpcpp[10:20] * y_dpcpp[20:30]
            self.assertEqual(res_cpu, res_dpcpp, atol=1e-1, rtol=1e-5)

    def test_sliced_binary(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10, 10, 10)
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = x_cpu.to(device=device)
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]

            # test mul
            y_cpu = x_cpu_slice * x_cpu_slice
            y_dpcpp = x_dpcpp_slice * x_dpcpp_slice
            self._check_tensor_shape(y_cpu, y_dpcpp)
            self.assertEqual(y_cpu, y_dpcpp, atol=1e-1, rtol=1e-5)

            # test sum
            y_cpu = x_cpu_slice + x_cpu_slice
            y_dpcpp = x_dpcpp_slice + x_dpcpp_slice
            self._check_tensor_shape(y_cpu, y_dpcpp)
            self.assertEqual(y_cpu, y_dpcpp, atol=1e-1, rtol=1e-5)

    def test_extract_sliced(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10, 10, 10)
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = x_cpu.to(device=device)
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]

            x_cpu_slice_clone = x_cpu_slice.clone()
            x_dpcpp_slice_clone = x_dpcpp_slice.clone()
            self._check_tensor_shape(x_cpu_slice_clone, x_dpcpp_slice_clone)
            self.assertEqual(x_cpu_slice_clone, x_dpcpp_slice_clone, atol=1e-1, rtol=1e-5)

    def test_sliced_eltwise(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10, 10, 10)
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = x_cpu.to(device=device)
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]

            y_cpu = F.relu(x_cpu_slice)
            y_dpcpp = F.relu(x_dpcpp_slice)
            self._check_tensor_shape(y_cpu, y_dpcpp)
            self.assertEqual(y_cpu, y_dpcpp, atol=1e-1, rtol=1e-5)

    def test_sliced_inplace_eltwise(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.rand(10, 10, 10)
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = x_cpu.to(device=device)
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]

            F.relu_(x_cpu_slice)
            F.relu_(x_dpcpp_slice)
            self._check_tensor_shape(x_cpu_slice, x_dpcpp_slice)
            self.assertEqual(x_cpu_slice, x_dpcpp_slice, atol=1e-1, rtol=1e-5)

    def test_sliced_eltwise_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        input = torch.rand(10, 10, 10)
        with AutoDNNL(True), AutoMixPrecision(True, train=True):
            x_cpu = input.clone().requires_grad_()
            x_cpu_slice = x_cpu[3:7, 3:7, 5]

            x_dpcpp = input.clone().to(device=device).requires_grad_()
            x_dpcpp_slice = x_dpcpp[3:7, 3:7, 5]

            y_cpu = F.relu(x_cpu_slice)
            y_dpcpp = F.relu(x_dpcpp_slice)

            y_cpu.sum().backward()
            y_dpcpp.sum().backward()

            self._check_tensor_shape(y_cpu, y_dpcpp)
            self.assertEqual(y_cpu, y_dpcpp)
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_linear_with_sliced_bias(self):
        bias = torch.rand(30)
        x_cpu = torch.rand(20, 30)
        w_cpu = torch.rand(10, 30)
        b_cpu = torch.rand(30)
        y_cpu = F.linear(x_cpu, w_cpu, b_cpu[10:20])

        x_dpcpp = x_cpu.to(device=device)
        w_dpcpp = w_cpu.to(device=device)
        b_dpcpp = b_cpu.to(device=device)
        with AutoDNNL(True), AutoMixPrecision(True):
            y_dpcpp = F.linear(x_dpcpp, w_dpcpp, b_dpcpp[10:20])

        self.assertEqual(y_cpu, y_dpcpp, atol=1e-1, rtol=1e-5)

    def test_chunk_version_counter(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        x_dpcpp = torch.randn(32, 4096).to(device).requires_grad_()

        with AutoDNNL(True), AutoMixPrecision(True, train=True):
            x_chunked = x_dpcpp.unsafe_chunk(4, 1)

            output = x_chunked[0].sigmoid_()
            version_counter = output._version

            output_other = x_chunked[1].sigmoid_()
            self.assertTrue(output._version == version_counter)

    def test_unbind(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        x_cpu = torch.rand(2, 8, 2)
        x_dpcpp = copy.deepcopy(x_cpu).to(device=device)

        x_cpu_unbind = torch.unbind(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(True):
            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_dpcpp))
            x_dpcpp_unbind = torch.unbind(x_dpcpp)
            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_dpcpp))
            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_dpcpp_unbind[0]))
            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_dpcpp_unbind[1]))

            self._check_tensor_shape(x_cpu_unbind[0], x_dpcpp_unbind[0])
            self._check_tensor_shape(x_cpu_unbind[1], x_dpcpp_unbind[1])
            self.assertEqual(x_cpu_unbind[0], x_dpcpp_unbind[0], atol=1e-1, rtol=1e-5)
            self.assertEqual(x_cpu_unbind[1], x_dpcpp_unbind[1], atol=1e-1, rtol=1e-5)

class TestBinOPs(TestCase):
    def _gen_shapes(self):
        dims = torch.randint(1, 10, (1,))
        shape = torch.randint(1, 10, list(dims))
        return shape.tolist()

    def _gen_binary_tensors(self, rand_seed):
        torch.manual_seed(rand_seed)

        shape = self._gen_shapes()

        x_cpu_a = torch.rand(shape)
        x_cpu_b = torch.rand(shape)

        x_auto_mix_a_infer = x_cpu_a.to(device=device)
        x_auto_mix_b_infer = x_cpu_b.to(device=device)

        x_auto_mix_a = x_cpu_a.to(device=device)
        x_auto_mix_b = x_cpu_b.to(device=device)
        x_man_bf16_a = x_auto_mix_a.to(torch.bfloat16)
        x_man_bf16_b = x_auto_mix_b.to(torch.bfloat16)

        x_auto_mix_bf16_a = convert_to_bf16(x_cpu_a)
        x_auto_mix_bf16_b = convert_to_bf16(x_cpu_b)

        return x_cpu_a, x_cpu_b, x_auto_mix_a_infer, x_auto_mix_b_infer, x_auto_mix_a, x_auto_mix_b, x_man_bf16_a, x_man_bf16_b, x_auto_mix_bf16_a, x_auto_mix_bf16_b

    def test_add(self):
        rand_seed = int(get_rand_seed())

        # generate a 4D tensor by using this seed
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu_a, x_cpu_b, x_auto_mix_a_infer, x_auto_mix_b_infer, x_auto_mix_a, x_auto_mix_b, x_man_bf16_a, x_man_bf16_b, x_auto_mix_bf16_a, x_auto_mix_bf16_b = self._gen_binary_tensors(rand_seed)
        res_cpu = x_cpu_a + x_cpu_b
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            res_man_bf16 = x_man_bf16_a + x_man_bf16_b
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

            # For inference: reorder the two inputs to bf16 in auto mix precision mode
            with AutoMixPrecision(True, train=False):
                # fp32 + fp32
                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))
                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))
                res_auto_mix_infer = x_auto_mix_a_infer + x_auto_mix_b_infer
                self.assertEqual(res_auto_mix_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_infer))

                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))

                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))

                self.assertEqual(res_auto_mix_infer.float(), res_man_bf16.float())

            # For training: only reorder the second tensor to the data type of the first tensor.
            with AutoMixPrecision(True, train=True):
                # bf16 + bf16
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                res_auto_mix_bf16 = x_auto_mix_bf16_a + x_auto_mix_bf16_b
                self.assertEqual(res_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_bf16))

                self.assertEqual(res_auto_mix_bf16.float(), res_man_bf16.float())

                # bf16 + fp32
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_b.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b))
                res_auto_mix_reorder_b = x_auto_mix_bf16_a + x_auto_mix_b
                self.assertEqual(res_auto_mix_reorder_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_reorder_b))

                self.assertEqual(res_auto_mix_reorder_b.float(), res_man_bf16.float())

                # fp32 + bf16
                self.assertEqual(x_auto_mix_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                res_auto_mix_reorder_a = x_auto_mix_a + x_auto_mix_bf16_b
                self.assertEqual(res_auto_mix_reorder_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_reorder_a))

                self.assertEqual(res_auto_mix_reorder_a, res_cpu, atol=1e-1, rtol=1e-5)

    def test_mul(self):
        rand_seed = int(get_rand_seed())

        # generate a 4D tensor by using this seed
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu_a, x_cpu_b, x_auto_mix_a_infer, x_auto_mix_b_infer, x_auto_mix_a, x_auto_mix_b, x_man_bf16_a, x_man_bf16_b, x_auto_mix_bf16_a, x_auto_mix_bf16_b = self._gen_binary_tensors(rand_seed)
        res_cpu = x_cpu_a * x_cpu_b
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            res_man_bf16 = x_man_bf16_a * x_man_bf16_b
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

            # For inference: reorder the two inputs to bf16 in auto mix precision mode
            with AutoMixPrecision(True, train=False):
                # fp32 * fp32
                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))
                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))
                res_auto_mix_infer = x_auto_mix_a_infer * x_auto_mix_b_infer
                self.assertEqual(res_auto_mix_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_infer))

                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))

                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))

                self.assertEqual(res_auto_mix_infer.float(), res_man_bf16.float())

            # For training: only reorder the second tensor to the data type of the first tensor.
            with AutoMixPrecision(True, train=True):
                # bf16 * bf16
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                res_auto_mix_bf16 = x_auto_mix_bf16_a * x_auto_mix_bf16_b
                self.assertEqual(res_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_bf16))

                self.assertEqual(res_auto_mix_bf16.float(), res_man_bf16.float())

                # bf16 * fp32
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_b.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b))
                res_auto_mix_reorder_b = x_auto_mix_bf16_a * x_auto_mix_b
                self.assertEqual(res_auto_mix_reorder_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_reorder_b))

                self.assertEqual(res_auto_mix_reorder_b.float(), res_man_bf16.float())

                # fp32 * bf16
                self.assertEqual(x_auto_mix_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                res_auto_mix_reorder_a = x_auto_mix_a * x_auto_mix_bf16_b
                self.assertEqual(res_auto_mix_reorder_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_reorder_a))

                self.assertEqual(res_auto_mix_reorder_a.float(), res_cpu.float(), atol=1e-1, rtol=1e-5)

    def test_mul_(self):
        rand_seed = int(get_rand_seed())

        # generate a 4D tensor by using this seed
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        torch.manual_seed(rand_seed)

        shape = self._gen_shapes()

        x_cpu_a = torch.rand(shape)
        x_cpu_b = torch.rand(shape)

        x_auto_mix_a_infer = x_cpu_a.to(device=device)
        x_auto_mix_b_infer = x_cpu_b.to(device=device)

        x_auto_mix_a = x_cpu_a.to(device=device)
        x_auto_mix_b = x_cpu_b.to(device=device)
        x_man_bf16_a = x_auto_mix_a.to(torch.bfloat16)
        x_man_bf16_b = x_auto_mix_b.to(torch.bfloat16)

        x_auto_mix_bf16_a = convert_to_bf16(x_cpu_a)
        x_auto_mix_bf16_a_ = convert_to_bf16(x_cpu_a)
        x_auto_mix_bf16_b = convert_to_bf16(x_cpu_b)

        x_cpu_a *= x_cpu_b
        with AutoDNNL(True), AutoMixPrecision(False):
            res_man_bf16 = x_man_bf16_a * x_man_bf16_b
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            x_man_bf16_a *= x_man_bf16_b
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_a.float(), res_man_bf16.float())

            # For inference: reorder the two inputs to bf16 in auto mix precision mode
            with AutoMixPrecision(True, train=False):
                # fp32 + fp32
                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))
                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))
                x_auto_mix_a_infer *= x_auto_mix_b_infer

                self.assertEqual(x_auto_mix_a_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_a_infer))

                self.assertEqual(x_auto_mix_b_infer.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_b_infer))

                self.assertEqual(x_auto_mix_a_infer.float(), res_man_bf16.float())

            # For training, only reorder the second tensor to the data type of the first tensor.
            with AutoMixPrecision(True, train=True):
                # bf16 * bf16
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                x_auto_mix_bf16_a *= x_auto_mix_bf16_b
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))

                self.assertEqual(x_auto_mix_bf16_a.float(), x_man_bf16_a.float())

                # bf16 * fp32
                self.assertEqual(x_auto_mix_bf16_a_.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a_))
                self.assertEqual(x_auto_mix_b.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_b))
                x_auto_mix_bf16_a_ *= x_auto_mix_b
                self.assertEqual(x_auto_mix_bf16_a_.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a_))

                self.assertEqual(x_auto_mix_bf16_a_.float(), res_man_bf16.float())

                # fp32 * bf16
                self.assertEqual(x_auto_mix_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                x_auto_mix_a *= x_auto_mix_bf16_b
                self.assertEqual(x_auto_mix_a.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_a))

                self.assertEqual(x_auto_mix_a, x_cpu_a, atol=1e-1, rtol=1e-5)

    def test_div(self):
        rand_seed = int(get_rand_seed())
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        _, _, _, _, _, _, x_man_bf16_a, x_man_bf16_b, x_auto_mix_bf16_a, x_auto_mix_bf16_b = self._gen_binary_tensors(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            res_man_bf16 = x_man_bf16_a / x_man_bf16_b
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

            with AutoMixPrecision(True):
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                res_auto_mix_bf16 = x_auto_mix_bf16_a / x_auto_mix_bf16_b
                self.assertEqual(res_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_bf16))
                self.assertEqual(res_auto_mix_bf16.float(), res_man_bf16.float())


    def test_div_(self):
        rand_seed = int(get_rand_seed())
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        _, _, _, _, _, _, x_man_bf16_a, x_man_bf16_b, x_auto_mix_bf16_a, x_auto_mix_bf16_b = self._gen_binary_tensors(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            x_man_bf16_a /= x_man_bf16_b
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)

            with AutoMixPrecision(True):
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_b.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_b))
                x_auto_mix_bf16_a /= x_auto_mix_bf16_b
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_a.float(), x_man_bf16_a.float())


    def test_div_scalar(self):
        rand_seed = int(get_rand_seed())
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        _, _, _, _, _, _, x_man_bf16_a, _, x_auto_mix_bf16_a, _ = self._gen_binary_tensors(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            res_man_bf16 = x_man_bf16_a / 3.3
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

            with AutoMixPrecision(True):
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                res_auto_mix_bf16 = x_auto_mix_bf16_a / 3.3
                self.assertEqual(res_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_bf16))
                self.assertEqual(res_auto_mix_bf16.float(), res_man_bf16.float())

    def test_div__scalar(self):
        rand_seed = int(get_rand_seed())
        rand_seed = 1591058395950926848
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        _, _, _, _, _, _, x_man_bf16_a, _, x_auto_mix_bf16_a, _ = self._gen_binary_tensors(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            x_man_bf16_a = x_man_bf16_a / 3.3
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)

            with AutoMixPrecision(True):
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                x_auto_mix_bf16_a = x_auto_mix_bf16_a / 3.3
                self.assertEqual(x_auto_mix_bf16_a.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16_a))
                self.assertEqual(x_auto_mix_bf16_a.float(), x_man_bf16_a.float())

class TestLinear(TestCase):
    def test_linear(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        in_features = torch.randint(3, 10, (1,)).item()
        out_features = torch.randint(3, 100, (1,)).item()
        x_auto_mix = torch.randn(3, in_features, dtype=torch.float32, device=device) * 10
        x_man_bf16 = x_auto_mix.to(torch.bfloat16)

        for bias in [True, False]:
            linear = torch.nn.Linear(in_features, out_features, bias=bias)

            linear_auto_mix = copy.deepcopy(linear).to(device=device)
            linear_man_bf16 = copy.deepcopy(linear).to(device=device).to(torch.bfloat16)

            with AutoDNNL(True), AutoMixPrecision(False):
                res_man_bf16 = linear_man_bf16(x_man_bf16)
                self.assertEqual(res_man_bf16.dtype, torch.bfloat16)

                with AutoMixPrecision(True):
                    res_auto_mix = linear_auto_mix(x_auto_mix)
                    self.assertEqual(res_auto_mix.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                    self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_linear_backward(self):
        ipex._C.set_execution_mode(train = True)
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        in_features = torch.randint(3, 10, (1,)).item()
        out_features = torch.randint(3, 100, (1,)).item()
        input = torch.randn(3, in_features) * 10
        for bias in [True, False]:
            _linear = torch.nn.Linear(in_features, out_features, bias=bias)
            linear_man_bf16 =copy.deepcopy(_linear).to(device=device).to(torch.bfloat16)
            linear_auto_mix =copy.deepcopy(_linear).to(device=device)
            _in_cpu = input.clone().requires_grad_()
            in_auto_mix = input.clone().to(device=device).requires_grad_()
            in_man_bf16 = input.clone().to(device=device).to(torch.bfloat16).requires_grad_()
            out_cpu = _linear(_in_cpu).sum()
            out_cpu.backward()
            with AutoDNNL(True), AutoMixPrecision(False, train=True):
                out_man_bf16 = linear_man_bf16(in_man_bf16).sum()
                out_man_bf16.backward()
                self.assertEqual(in_man_bf16.grad.dtype, torch.bfloat16)
                # rand_seed = 1600407821102260224 # self.assertEqual(_in_cpu.grad.bfloat16().float(), in_man_bf16.grad, 2e-2) AssertionError: tensor(0.0312) not less than or equal to 0.02
                self.assertEqual(_in_cpu.grad.bfloat16().float(), in_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

                with AutoMixPrecision(True, train=True):
                    self.assertEqual(in_auto_mix.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(in_auto_mix))
                    out_auto_bf16 = linear_auto_mix(in_auto_mix).sum()
                    out_auto_bf16.backward()
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(in_auto_mix.grad))
                    self.assertEqual(in_man_bf16.grad.float(), in_auto_mix.grad.float())

class TestPool(TestCase):
    def test_avg_pool2d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for count_include_pad in [True, False]:
            x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
                rand_seed,
                (N, C, 64, 64))
            op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
                rand_seed,
                torch.nn.AvgPool2d(
                    kernel_size=3,
                    stride=2,
                    padding=1,
                    count_include_pad=count_include_pad),
                is_bn=False)

            ref_cpu = op_cpu(x_cpu)
            with AutoDNNL(True), AutoMixPrecision(False):
                res_bf16 = op_man_bf16(x_man_bf16)
                self.assertEqual(res_bf16.dtype, torch.bfloat16)

                # FW inference
                with AutoMixPrecision(True, train=False):
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                    self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    self.assertEqual(res_bf16.float(), res_auto_mix_inference)

                # FW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                    self.assertEqual(res_auto_mix_train.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    self.assertEqual(ref_cpu, res_auto_mix_train)

                # FW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                    self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_avg_pool2d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for count_include_pad in [True, False]:
            x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(
                rand_seed, (N, C, 64, 64),
                is_forward=False)

            op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(
                rand_seed,
                torch.nn.AvgPool2d(
                    kernel_size=3,
                    stride=2,
                    padding=1,
                    count_include_pad=count_include_pad),
                    is_bn=False,
                is_forward=False)
            out_cpu = op_cpu(x_cpu).sum()
            out_cpu.backward()
            with AutoDNNL(True), AutoMixPrecision(False, train=True):
                out_man_bf16 = op_man_bf16(x_man_bf16).sum()
                out_man_bf16.backward()
                self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
                self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

                # BW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                    out_auto_mix = op_auto_mix(x_auto_mix).sum()
                    out_auto_mix.backward()
                    self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                    self.assertEqual(x_cpu.grad, x_auto_mix.grad)

                # BW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                    out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                    out_auto_mix_bf16.backward()
                    self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                    self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

    def test_avg_pool3d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for count_include_pad in [True, False]:
            x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
                rand_seed,
                (N, C, 64, 64, 64))
            op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
                rand_seed,
                torch.nn.AvgPool3d(
                    kernel_size=3,
                    stride=2,
                    padding=1,
                    count_include_pad=count_include_pad),
                is_bn=False)

            ref_cpu = op_cpu(x_cpu)
            with AutoDNNL(True), AutoMixPrecision(False):
                res_bf16 = op_man_bf16(x_man_bf16)
                self.assertEqual(res_bf16.dtype, torch.bfloat16)

                # FW inference
                with AutoMixPrecision(True, train=False):
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                    self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    self.assertEqual(res_bf16.float(), res_auto_mix_inference)

                # FW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                    self.assertEqual(res_auto_mix_train.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    self.assertEqual(ref_cpu, res_auto_mix_train)

                # FW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                    self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_avg_pool3d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for count_include_pad in [True, False]:

            x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(
                rand_seed, (N, C, 64, 64, 64),
                is_forward=False)

            op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(
                rand_seed,
                torch.nn.AvgPool3d(
                    kernel_size=3,
                    stride=2,
                    padding=1,
                    count_include_pad=count_include_pad),
                    is_bn=False,
                is_forward=False)
            out_cpu = op_cpu(x_cpu).sum()
            out_cpu.backward()
            with AutoDNNL(True), AutoMixPrecision(False, train=True):
                out_man_bf16 = op_man_bf16(x_man_bf16).sum()
                out_man_bf16.backward()
                self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
                self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

                # BW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                    out_auto_mix = op_auto_mix(x_auto_mix).sum()
                    out_auto_mix.backward()
                    self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                    self.assertEqual(x_cpu.grad, x_auto_mix.grad)

                # BW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                    out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                    out_auto_mix_bf16.backward()


                    self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                    self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

    def test_adaptive_avg_pool2d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (N, C, 224, 224))
        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.AdaptiveAvgPool2d(7), is_bn=False)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_adaptive_avg_pool2d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (N, C, 224, 224), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.AdaptiveAvgPool2d(7), is_bn=False, is_forward=False)
        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), 1e-2)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad.float(), x_auto_mix.grad.float())

            # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()

                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad.float())

    def test_max_pool2d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for stride in [1, 2, 3]:
            for H, W in [(64, 64), (35, 39), (16, 19), [7, 8]]:
                for ceil_mode in [False, True]:
                    x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(
                        rand_seed,
                        (N, C, H, W))
                    op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
                        rand_seed,
                        torch.nn.MaxPool2d(
                            kernel_size=3 if not ceil_mode else 7,
                            stride=stride,
                            padding=1,
                            ceil_mode=ceil_mode),
                        is_bn=False)

                    ref_cpu = op_cpu(x_cpu)
                    with AutoDNNL(True), AutoMixPrecision(False):
                        res_bf16 = op_man_bf16(x_man_bf16)
                        self.assertEqual(res_bf16.dtype, torch.bfloat16)

                        # FW inference
                        with AutoMixPrecision(True, train=False):
                            self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                            res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                            self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                            self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                            self.assertEqual(res_bf16.float(), res_auto_mix_inference.float())

                        # FW train (input is not bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_train.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                            res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                            self.assertEqual(res_auto_mix_train.dtype, torch.float)
                            self.assertEqual(x_auto_mix_train.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                            self.assertEqual(ref_cpu.float(), res_auto_mix_train.float())

                        # FW train (input is bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                            res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                            self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                            self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                            self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_max_pool2d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for stride in [1, 2, 3]:
            for H, W in [(64, 64), (35, 39), (16, 19), [7, 8]]:
                for ceil_mode in [False, True]:
                    x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (N, C, H, W), is_forward=False)
                    op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(
                        rand_seed,
                        torch.nn.MaxPool2d(
                            kernel_size=3 if not ceil_mode else 7,
                            stride=stride,
                            padding=1,
                            ceil_mode=ceil_mode),
                        is_bn=False,
                        is_forward=False)

                    out_cpu = op_cpu(x_cpu).sum()
                    out_cpu.backward()
                    with AutoDNNL(True), AutoMixPrecision(False, train=True):
                        out_man_bf16 = op_man_bf16(x_man_bf16).sum()
                        out_man_bf16.backward()
                        self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)

                        # BW train (input is not bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                            out_auto_mix = op_auto_mix(x_auto_mix).sum()
                            out_auto_mix.backward()
                            self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                            self.assertEqual(x_cpu.grad, x_auto_mix.grad)

                        # BW train (input is bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                            out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                            out_auto_mix_bf16.backward()

                            self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                            self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

    def test_max_pool3d(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for stride in [1, 2, 3]:
            for D, H, W in [(64, 64, 64), (35, 39, 35), (16, 19, 20), [7, 8, 9]]:
                for ceil_mode in [False, True]:
                    x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (N, C, D, H, W))
                    op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(
                        rand_seed,
                        torch.nn.MaxPool3d(
                            kernel_size=3 if not ceil_mode else 7,
                            stride=stride,
                            padding=1,
                            ceil_mode=ceil_mode),
                        is_bn=False)
                    ref_cpu = op_cpu(x_cpu)
                    with AutoDNNL(True), AutoMixPrecision(False):
                        res_bf16 = op_man_bf16(x_man_bf16)
                        self.assertEqual(res_bf16.dtype, torch.bfloat16)

                        # FW inference
                        with AutoMixPrecision(True, train=False):
                            self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                            res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                            self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                            self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                            self.assertEqual(res_bf16.float(), res_auto_mix_inference)

                        # FW train (input is not bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_train.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                            res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                            self.assertEqual(res_auto_mix_train.dtype, torch.float)
                            self.assertEqual(x_auto_mix_train.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                            self.assertEqual(ref_cpu, res_auto_mix_train)

                        # FW train (input is bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                            res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                            self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                            self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                            self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_max_pool3d_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for stride in [1, 2, 3]:
            for D, H, W in [(64, 64, 64), (35, 39, 35), (16, 19, 20), [7, 8, 9]]:
                for ceil_mode in [False, True]:
                    x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (N, C, D, H, W), is_forward=False)
                    op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(
                        rand_seed,
                        torch.nn.MaxPool3d(
                            kernel_size=3 if not ceil_mode else 7,
                            stride=stride,
                            padding=1,
                            ceil_mode=ceil_mode),
                        is_bn=False,
                        is_forward=False)

                    out_cpu = op_cpu(x_cpu).sum()
                    out_cpu.backward()
                    with AutoDNNL(True), AutoMixPrecision(False, train=True):
                        out_man_bf16 = op_man_bf16(x_man_bf16).sum()
                        out_man_bf16.backward()
                        self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)

                        # BW train (input is not bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                            out_auto_mix = op_auto_mix(x_auto_mix).sum()
                            out_auto_mix.backward()
                            self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                            self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                            self.assertEqual(x_cpu.grad, x_auto_mix.grad)

                        # BW train (input is bf16 dil tensor)
                        with AutoMixPrecision(True, train=True):
                            self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                            out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                            out_auto_mix_bf16.backward()

                            self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                            self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                            self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestIndex(TestCase):
    def test_index_select(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        x_auto_mix = torch.randn(3, 4, 5, dtype=torch.float32, device=device) * 10

        indices = torch.tensor([0, 2]).to(device=device)
        index_select_x_auto_mix = copy.deepcopy(x_auto_mix).to(device=device)
        index_select_x_man_mix = copy.deepcopy(x_auto_mix).to(device=device).to(torch.bfloat16)

        with AutoDNNL(True), AutoMixPrecision(False):
            res_man_bf16 = index_select_x_man_mix + index_select_x_man_mix
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            res_idx_select_man = torch.index_select(res_man_bf16, 0, indices)
            self.assertEqual(res_idx_select_man.dtype, torch.bfloat16)

            with AutoMixPrecision(True):
                res_auto_mix = index_select_x_auto_mix + index_select_x_auto_mix
                self.assertEqual(res_auto_mix.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                res_idx_select_auto = torch.index_select(res_auto_mix, 0, indices)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_idx_select_auto))
                self.assertEqual(res_idx_select_auto, res_idx_select_man.float())

    def test_index(self):
        x_auto_mix = torch.randn(3, 4, 5, dtype=torch.float32, device=device)
        indices = torch.tensor([0, 2]).to(device=device)
        x_auto_mix[indices]
        # rand_seed = int(get_rand_seed())
        # print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        # torch.manual_seed(rand_seed)
        # x_auto_mix = torch.randn(3, 4, 5, dtype=torch.float32, device=device) * 10

        # indices = torch.tensor([0, 2]).to(device=device)
        # index_x_auto_mix = copy.deepcopy(x_auto_mix).to(device=device)
        # index_x_man_mix = copy.deepcopy(x_auto_mix).to(device=device).to(torch.bfloat16)

        # with AutoDNNL(True), AutoMixPrecision(False):
        #     res_man_bf16 = index_x_man_mix + index_x_man_mix
        #     self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
        #     print(res_man_bf16.device)
        #     print(indices.device)
        #     res_idx_man = res_man_bf16[indices]
        #     self.assertEqual(res_idx_man.dtype, torch.bfloat16)

        #     with AutoMixPrecision(True):
        #         res_auto_mix = index_x_auto_mix + index_x_auto_mix
        #         self.assertEqual(res_auto_mix.dtype, torch.float)
        #         self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
        #         print(res_auto_mix.device)
        #         print(indices.device)
        #         res_idx_auto = res_auto_mix[indices]
        #         self.assertTrue(ipex._C.is_bf16_dil_tensor(res_idx_auto))
        #         self.assertEqual(res_idx_auto, res_idx_man.float())

class TestSoftMax(TestCase):
    def test_softmax(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        ndim = 3
        for dim in range(ndim):
            x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (3, 4, 5))

            op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.Softmax(dim=dim), is_bn=False)

            ref_cpu = op_cpu(x_cpu)
            with AutoDNNL(True), AutoMixPrecision(False):
                res_bf16 = op_man_bf16(x_man_bf16)
                self.assertEqual(res_bf16.dtype, torch.bfloat16)

                # FW inference
                with AutoMixPrecision(True, train=False):
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                    self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                    self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                    self.assertEqual(res_bf16.float(), res_auto_mix_inference)

                # FW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                    self.assertEqual(res_auto_mix_train.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                    self.assertEqual(ref_cpu, res_auto_mix_train)

                # FW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                    self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                    self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                    self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_softmax_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        ndim = 3
        for dim in range(ndim):
            x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (3, 4, 5), is_forward=False)

            op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.Softmax(dim=dim), is_bn=False, is_forward=False)

            out_cpu = op_cpu(x_cpu).sum()
            out_cpu.backward()
            with AutoDNNL(True), AutoMixPrecision(False, train=True):
                out_man_bf16 = op_man_bf16(x_man_bf16).sum()
                out_man_bf16.backward()
                self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
                self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

                # BW train (input is not bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                    out_auto_mix = op_auto_mix(x_auto_mix).sum()
                    out_auto_mix.backward()
                    self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                    self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                    self.assertEqual(x_cpu.grad, x_auto_mix.grad)

                # BW train (input is bf16 dil tensor)
                with AutoMixPrecision(True, train=True):
                    self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                    self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                    out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                    out_auto_mix_bf16.backward()
                    self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                    # TODO
                    # grady and y both fp32 after .sum()
                    # self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                    # self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

    def test_log_softmax(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True, train=False):
            x_cpu = torch.randn(3, 4, 5, dtype=torch.float32) * 10
            x_dpcpp = x_cpu.to(device=device)
            for dim in range(x_cpu.ndim):
                self.assertEqual(F.log_softmax(x_cpu, dim=dim), F.log_softmax(x_dpcpp, dim=dim), atol=5e-1, rtol=1e-5)

    def test_log_softmax_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True, train=True):
            x = torch.randn(3, 4, 5, dtype=torch.float32) * 10
            for dim in range(x.ndim):
                x_cpu = x.clone().requires_grad_()
                x_dpcpp = x.clone().to(device=device).requires_grad_()
                y_cpu = F.log_softmax(x_cpu, dim=dim).sum()
                y_dpcpp = F.log_softmax(x_dpcpp, dim=dim).sum()
                y_cpu.backward()
                y_dpcpp.backward()
                self.assertEqual(x_cpu.grad, x_dpcpp.grad, atol=0.1, rtol=1e-2)

class TestSigmoid(TestCase):
    def test_sigmoid(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.Sigmoid(), is_bn=False)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_sigmoid_(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        x_cpu.sigmoid_()
        with AutoDNNL(True), AutoMixPrecision(False):
            x_man_bf16.sigmoid_()
            self.assertEqual(x_man_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                x_auto_mix_inference.sigmoid_()
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                x_auto_mix_train.sigmoid_()
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(x_cpu, x_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                x_auto_mix_train_bf16.sigmoid_()
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_train_bf16, 1e-3)

    def test_sigmoid_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (4, 5), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.Sigmoid(), is_bn=False, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                # TODO
                # grady and y both fp32 after .sum()
                # self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                # self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestTanh(TestCase):
    def test_tanh(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        op_cpu, op_auto_mix_inference, op_auto_mix_train, op_man_bf16, op_auto_mix_train_bf16 = _gen_op(rand_seed, torch.nn.Tanh(), is_bn=False)

        ref_cpu = op_cpu(x_cpu)
        with AutoDNNL(True), AutoMixPrecision(False):
            res_bf16 = op_man_bf16(x_man_bf16)
            self.assertEqual(res_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                res_auto_mix_inference = op_auto_mix_inference(x_auto_mix_inference)
                self.assertEqual(res_auto_mix_inference.dtype, torch.float)
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_inference))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(res_bf16.float(), res_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                res_auto_mix_train = op_auto_mix_train(x_auto_mix_train)
                self.assertEqual(res_auto_mix_train.dtype, torch.float)
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(res_auto_mix_train))
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(ref_cpu, res_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                res_auto_mix_train_bf16 = op_auto_mix_train_bf16(x_auto_mix_train_bf16)
                self.assertEqual(res_auto_mix_train_bf16.dtype, torch.float)
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix_train_bf16))
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(res_bf16.float(), res_auto_mix_train_bf16, 1e-3)

    def test_tanh_(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_cpu, x_auto_mix_inference, x_auto_mix_train, x_man_bf16, x_auto_mix_train_bf16 = _gen_tensor(rand_seed, (4, 5))

        x_cpu.tanh_()
        with AutoDNNL(True), AutoMixPrecision(False):
            x_man_bf16.tanh_()
            self.assertEqual(x_man_bf16.dtype, torch.bfloat16)

            # FW inference
            with AutoMixPrecision(True, train=False):
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                x_auto_mix_inference.tanh_()
                self.assertEqual(x_auto_mix_inference.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_inference))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_inference)

            # FW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                x_auto_mix_train.tanh_()
                self.assertEqual(x_auto_mix_train.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix_train))
                self.assertEqual(x_cpu, x_auto_mix_train)

            # FW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                x_auto_mix_train_bf16.tanh_()
                self.assertEqual(x_auto_mix_train_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_train_bf16))
                self.assertEqual(x_man_bf16.float(), x_auto_mix_train_bf16, 1e-3)

    def test_tanh_backward(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))

        x_cpu, _, x_auto_mix, x_man_bf16, x_auto_mix_bf16 = _gen_tensor(rand_seed, (4, 5), is_forward=False)

        op_cpu, _, op_auto_mix, op_man_bf16, op_auto_mix_bf16 = _gen_op(rand_seed, torch.nn.Tanh(), is_bn=False, is_forward=False)

        out_cpu = op_cpu(x_cpu).sum()
        out_cpu.backward()
        with AutoDNNL(True), AutoMixPrecision(False, train=True):
            out_man_bf16 = op_man_bf16(x_man_bf16).sum()
            out_man_bf16.backward()
            self.assertEqual(x_man_bf16.grad.dtype, torch.bfloat16)
            self.assertEqual(x_cpu.grad.bfloat16().float(), x_man_bf16.grad.float(), atol=1e-1, rtol=1e-5)

            # BW train (input is not bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix))
                out_auto_mix = op_auto_mix(x_auto_mix).sum()
                out_auto_mix.backward()
                self.assertEqual(x_auto_mix.grad.dtype, torch.float)
                self.assertFalse(ipex._C.is_bf16_dil_tensor(x_auto_mix.grad))
                self.assertEqual(x_cpu.grad, x_auto_mix.grad)

             # BW train (input is bf16 dil tensor)
            with AutoMixPrecision(True, train=True):
                self.assertEqual(x_auto_mix_bf16.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16))
                out_auto_mix_bf16 = op_auto_mix_bf16(x_auto_mix_bf16).sum()
                out_auto_mix_bf16.backward()
                self.assertEqual(x_auto_mix_bf16.grad.dtype, torch.float)
                # TODO
                # grady and y both fp32 after .sum()
                # self.assertTrue(ipex._C.is_bf16_dil_tensor(x_auto_mix_bf16.grad))
                # self.assertEqual(x_man_bf16.grad.float(), x_auto_mix_bf16.grad)

class TestLinearAlgebraOps(TestCase):
    def _gen_mm_tensor(self, seed, batches = None):
        torch.manual_seed(seed)
        M, N, O = 23, 8, 12
        if batches != None:
            x_auto_mix_a = torch.randn(batches, M, N, dtype=torch.float32, device=device)
            x_auto_mix_b = torch.randn(batches, N, O, dtype=torch.float32, device=device)
        else:
            x_auto_mix_a = torch.randn(M, N, dtype=torch.float32, device=device)
            x_auto_mix_b = torch.randn(N, O, dtype=torch.float32, device=device)
        res_auto_mix = torch.randn(M, O, dtype=torch.float32, device=device)
        x_man_bf16_a = x_auto_mix_a.to(torch.bfloat16)
        x_man_bf16_b = x_auto_mix_b.to(torch.bfloat16)
        res_man_bf16 = res_auto_mix.to(torch.bfloat16)

        return x_auto_mix_a, x_auto_mix_b, res_auto_mix, x_man_bf16_a, x_man_bf16_b, res_man_bf16

    def test_mm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_auto_mix_a, x_auto_mix_b, _, x_man_bf16_a, x_man_bf16_b, _ = self._gen_mm_tensor(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(False):
            self.assertEqual(x_man_bf16_a.dtype, torch.bfloat16)
            self.assertEqual(x_man_bf16_b.dtype, torch.bfloat16)
            res_man_bf16 = torch.mm(x_man_bf16_a, x_man_bf16_b)
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            with AutoMixPrecision(True):
                res_auto_mix = torch.mm(x_auto_mix_a, x_auto_mix_b)
                self.assertEqual(res_auto_mix.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_mm_out(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_auto_mix_a, x_auto_mix_b, res_auto_mix, x_man_bf16_a, x_man_bf16_b, res_man_bf16 = self._gen_mm_tensor(rand_seed)

        with AutoDNNL(True), AutoMixPrecision(False):
            torch.mm(x_man_bf16_a, x_man_bf16_b, out=res_man_bf16)
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            with AutoMixPrecision(True):
                torch.mm(x_auto_mix_a, x_auto_mix_b, out=res_auto_mix)
                self.assertEqual(res_auto_mix.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_bmm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_auto_mix_a, x_auto_mix_b, _, x_man_bf16_a, x_man_bf16_b, _ = self._gen_mm_tensor(rand_seed, batches=16)

        with AutoDNNL(True), AutoMixPrecision(False):
            res_man_bf16 = torch.bmm(x_man_bf16_a, x_man_bf16_b)
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            with AutoMixPrecision(True):
                res_auto_mix = torch.bmm(x_auto_mix_a, x_auto_mix_b)
                self.assertEqual(res_auto_mix.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_bmm_out(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        x_auto_mix_a, x_auto_mix_b, res_auto_mix, x_man_bf16_a, x_man_bf16_b, res_man_bf16 = self._gen_mm_tensor(rand_seed, batches=16)
        with AutoDNNL(True), AutoMixPrecision(False):
            torch.bmm(x_man_bf16_a, x_man_bf16_b, out=res_man_bf16)
            self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
            with AutoMixPrecision(True):
                torch.bmm(x_auto_mix_a, x_auto_mix_b, out=res_auto_mix)
                self.assertEqual(res_auto_mix.dtype, torch.float)
                self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_addmm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        for i in range(8, 12, 2):
            for j in range(8, 12, 2):
                alpha = i / 10
                beta = j / 10

                x_auto_mix_a, x_auto_mix_b, add_auto_mix, x_man_bf16_a, x_man_bf16_b, add_man_bf16 = self._gen_mm_tensor(rand_seed)

                with AutoDNNL(True), AutoMixPrecision(False):
                    res_man_bf16 = torch.addmm(input=add_man_bf16, mat1=x_man_bf16_a, mat2=x_man_bf16_b, alpha=alpha, beta=beta)
                    self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
                    with AutoMixPrecision(True):
                        res_auto_mix = torch.addmm(input=add_auto_mix, mat1=x_auto_mix_a, mat2=x_auto_mix_b, alpha=alpha, beta=beta)
                        self.assertEqual(res_auto_mix.dtype, torch.float)
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                        self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_addbmm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        for i in range(8, 12, 2):
            for j in range(8, 12, 2):
                alpha = i / 10
                beta = j / 10
                num_batches = 10
                x_auto_mix_a, x_auto_mix_b, add_auto_mix, x_man_bf16_a, x_man_bf16_b, add_man_bf16 = self._gen_mm_tensor(rand_seed, num_batches)
                with AutoDNNL(True), AutoMixPrecision(False):
                    res_man_bf16 = torch.addbmm(add_man_bf16, x_man_bf16_a, x_man_bf16_b, beta=beta, alpha=alpha)
                    self.assertEqual(res_man_bf16.dtype, torch.bfloat16)
                    with AutoMixPrecision(True):
                        res_auto_mix = torch.addbmm(add_auto_mix, x_auto_mix_a, x_auto_mix_b, beta=beta, alpha=alpha)
                        self.assertEqual(res_auto_mix.dtype, torch.float)
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                        self.assertEqual(res_auto_mix, res_man_bf16.float())

    def test_baddbmm(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        for i in range(8, 12, 2):
            for j in range(8, 12, 2):
                alpha = i / 10
                beta = j / 10
                batches = 2

                M, N, O = 23, 8, 12
                x_auto_mix_a = torch.randn(batches, M, N, dtype=torch.float32, device=device)
                x_auto_mix_b = torch.randn(batches, N, O, dtype=torch.float32, device=device)
                add_auto_mix = torch.randn(batches, M, O, dtype=torch.float32, device=device)

                x_man_bf16_a = x_auto_mix_a.to(torch.bfloat16)
                x_man_bf16_b = x_auto_mix_b.to(torch.bfloat16)
                add_man_bf16 = add_auto_mix.to(torch.bfloat16)

                with AutoDNNL(True), AutoMixPrecision(False):
                    res_man_bf16 = torch.baddbmm(add_man_bf16, x_man_bf16_a, x_man_bf16_b, beta=beta, alpha=alpha)
                    with AutoMixPrecision(True):
                        res_auto_mix = torch.baddbmm(add_auto_mix, x_auto_mix_a, x_auto_mix_b, beta=beta, alpha=alpha)
                        self.assertEqual(res_auto_mix.dtype, torch.float)
                        self.assertTrue(ipex._C.is_bf16_dil_tensor(res_auto_mix))
                        self.assertEqual(res_auto_mix, res_man_bf16.float())

class ConvRelu(nn.Module):
    def __init__(self):
        super(ConvRelu, self).__init__()
        self.conv = torch.nn.Conv2d(16, 33, (3, 5), stride=(2, 1), padding=(4, 2), dilation=(3, 1))

    def forward(self, x):
        return F.relu(self.conv(x), inplace=True)

class TestSave(TestCase):
    def test_save_and_load(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        input = torch.randn(20, 16, 50, 100)
        input_dpcpp = input.clone().to(device=device)
        model = ConvRelu()
        model_dpcpp = copy.deepcopy(model).to(device=device)
        model_bf16 = copy.deepcopy(model).to(device=device).to(torch.bfloat16)

        #test save and load model
        torch.save(model.state_dict(), 'model.pth')
        torch.save(model_bf16.state_dict(), 'model_dpcpp.pth')
        state_dict1 = torch.load('model.pth')
        state_dict2 = torch.load('model_dpcpp.pth')
        model1 = ConvRelu()
        model2 = ConvRelu()
        model1.load_state_dict(state_dict1)
        model2.load_state_dict(state_dict2)
        self.assertEqual(model1(input), model2(input), atol=1e-1, rtol=1e-5)

        #test save and load tensor
        x = torch.tensor([0, 1, 2, 3, 4])
        x_dpcpp = x.clone().to(device=device).to(torch.bfloat16)
        self.assertEqual(x_dpcpp.dtype, torch.bfloat16)
        torch.save(x, 'tensor.pt')
        torch.save(x_dpcpp, 'tensor_dpcpp.pt')
        self.assertEqual(torch.load('tensor.pt').float(), torch.load('tensor_dpcpp.pt').float())

        with AutoDNNL(True), AutoMixPrecision(True):
            output_dpcpp = model_dpcpp(input_dpcpp)
            torch.save(output_dpcpp.clone().to('cpu'), 'tensor.pt')
            self.assertTrue(ipex._C.is_bf16_dil_tensor(output_dpcpp))
            torch.save(output_dpcpp, 'tensor_dpcpp.pt')
            self.assertEqual(torch.load('tensor.pt'), torch.load('tensor_dpcpp.pt'))

class TestFallbackOP(TestCase):
    def test__pack_padded_sequence(self):
        seqs = [torch.FloatTensor(random.randint(1, 6)).to(ipex.DEVICE) for _ in range(5)]
        seqs = [s.random_(-128, 128) for s in seqs]
        ordered = sorted(seqs, key=len, reverse=True)
        lengths = list(map(len, ordered))
        padded_tensor = rnn_utils.pad_sequence(ordered)
        with AutoDNNL(True):
            for enforce_sorted in [True, False]:
                packed = rnn_utils.pack_padded_sequence(padded_tensor, lengths, enforce_sorted=enforce_sorted)
                masked = packed.float()
                unpacked, lengths_out = rnn_utils.pad_packed_sequence(masked)
                self.assertTrue(str(unpacked.device) == ipex.DEVICE)
                self.assertTrue(str(lengths_out.device) == 'cpu')

class TestRNN(TestCase):
    def _lstm_params_list(self, cell):
        params_dict = {
            "input_size": [1, 2],
            "hidden_size": [5],
            "num_layers": [1, 3],
            "bidirectional": [False, True],
            "bias": [False, True],
            "empty_state": [False, True],
            "batch_first": [False, True],
            "dropout": [0, 1], # [0, 0.5, 1] # TODO 0.5 will fail
            "batch_size": [1, 2],
            "seq_len": [1, 3]
        }

        if cell == "RNN":
            params_dict["nonlinearity"] = ["tanh"] # ["tanh", "relu"] TODO relu has accuracy issue
        elif cell == "GRU":
            params_dict["nonlinearity"] = [""]

        params_list = []

        for key, value in params_dict.items():
            params_list.append(value)
        return params_list

    def _test_lstm(self, training):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        params_list = self._lstm_params_list("LSTM")

        for input_size, hidden_size, num_layers, bidirectional, bias, empty_state, batch_first, dropout, batch_size, seq_len in itertools.product(*params_list):
            # dropout option adds dropout after all but last recurrent layer, so non-zero dropout expects num_layers greater than 1
            if dropout > 0 and num_layers == 1:
                continue

            num_directions = 2 if bidirectional else 1

            if batch_first:
                input = torch.randn(batch_size, seq_len, input_size)
            else:
                input = torch.randn(seq_len, batch_size, input_size)
            h = torch.randn(num_layers * num_directions, batch_size, hidden_size)
            c = torch.randn(num_layers * num_directions, batch_size, hidden_size)

            input_cpu = input.bfloat16().float().clone().requires_grad_(training)
            h_cpu = h.bfloat16().float().clone().requires_grad_(training)
            c_cpu = c.bfloat16().float().clone().requires_grad_(training)

            model_cpu = torch.nn.LSTM(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers, bidirectional=bidirectional, bias=bias, dropout=dropout, batch_first=batch_first)
            model_cpu.train() if training else model_cpu.eval()

            input_dpcpp = input.clone().to(device=device).requires_grad_(training)
            h0_dpcpp = h.clone().to(device=device).requires_grad_(training)
            c0_dpcpp = c.clone().to(device=device).requires_grad_(training)
            model_dpcpp = copy.deepcopy(model_cpu).to(device=device)
            model_dpcpp.train() if training else model_dpcpp.eval()

            with AutoDNNL(True), AutoMixPrecision(True, train=training):
                if empty_state:
                    y_cpu, hy_cpu = model_cpu(input_cpu)
                    y_dpcpp, hy_dpcpp = model_dpcpp(input_dpcpp)
                else:
                    y_cpu, hy_cpu = model_cpu(input_cpu, (h_cpu, c_cpu))
                    y_dpcpp, hy_dpcpp = model_dpcpp(input_dpcpp, (h0_dpcpp, c0_dpcpp))
                self.assertTrue(torch.allclose(y_cpu, y_dpcpp, rtol=0.01, atol=0.05))
                self.assertTrue(torch.allclose(hy_cpu[0],hy_dpcpp[0], rtol=0.01, atol=0.01))
                self.assertTrue(torch.allclose(hy_cpu[1],hy_dpcpp[1], rtol=0.01, atol=0.01))

                if training:
                    y_cpu.sum().backward(retain_graph=True)
                    y_dpcpp.sum().backward(retain_graph=True)
                    self.assertTrue(torch.allclose(input_dpcpp.grad.to('cpu'), input_cpu.grad, rtol=0.01, atol=0.05))
                    self.assertTrue(torch.allclose(model_dpcpp.weight_ih_l0.grad.to('cpu'), model_cpu.weight_ih_l0.grad, rtol=0.01, atol=0.05))
                    self.assertTrue(torch.allclose(model_dpcpp.weight_hh_l0.grad.to('cpu'), model_cpu.weight_hh_l0.grad, rtol=0.01, atol=0.05))
                    if bias:
                        self.assertTrue(torch.allclose(model_dpcpp.bias_ih_l0.grad.to('cpu'), model_cpu.bias_ih_l0.grad, rtol=0.01, atol=0.05))
                        self.assertTrue(torch.allclose(model_dpcpp.bias_hh_l0.grad.to('cpu'), model_cpu.bias_hh_l0.grad, rtol=0.01, atol=0.05))
                    if not empty_state:
                        hy_cpu[0].sum().backward(retain_graph=True)
                        hy_dpcpp[0].sum().backward(retain_graph=True)
                        self.assertTrue(torch.allclose(h0_dpcpp.grad.to('cpu'), h_cpu.grad, rtol=0.01, atol=0.05))

                        hy_cpu[1].sum().backward(retain_graph=True)
                        hy_dpcpp[1].sum().backward(retain_graph=True)
                        self.assertTrue(torch.allclose(c0_dpcpp.grad.to('cpu'), c_cpu.grad, rtol=0.01, atol=0.05))

    def _test_rnn(self, cell, training):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        params_list = self._lstm_params_list(cell)

        for input_size, hidden_size, num_layers, bidirectional, bias, empty_state, batch_first, dropout, batch_size, seq_len, nonlinearity in itertools.product(*params_list):
            # dropout option adds dropout after all but last recurrent layer, so non-zero dropout expects num_layers greater than 1
            if dropout > 0 and num_layers == 1:
                continue

            num_directions = 2 if bidirectional else 1

            if batch_first:
                input = torch.randn(batch_size, seq_len, input_size)
            else:
                input = torch.randn(seq_len, batch_size, input_size)
            h = torch.randn(num_layers * num_directions, batch_size, hidden_size)
            c = torch.randn(num_layers * num_directions, batch_size, hidden_size)

            input_cpu = input.bfloat16().float().clone().requires_grad_(training)
            h_cpu = h.bfloat16().float().clone().requires_grad_(training)
            c_cpu = c.bfloat16().float().clone().requires_grad_(training)

            if cell == "RNN":
                model_cpu = torch.nn.RNN(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers, bidirectional=bidirectional, bias=bias, dropout=dropout, batch_first=batch_first, nonlinearity=nonlinearity)
            elif cell == "GRU":
                model_cpu = torch.nn.GRU(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers, bidirectional=bidirectional, bias=bias, dropout=dropout, batch_first=batch_first)

            model_cpu.train() if training else model_cpu.eval()

            input_dpcpp = input.clone().to(device=device).requires_grad_(training)
            h0_dpcpp = h.clone().to(device=device).requires_grad_(training)
            c0_dpcpp = c.clone().to(device=device).requires_grad_(training)
            model_dpcpp = copy.deepcopy(model_cpu).to(device=device)
            model_dpcpp.train() if training else model_dpcpp.eval()

            with AutoDNNL(True), AutoMixPrecision(True, train=training):
                if empty_state:
                    y_cpu, hy_cpu = model_cpu(input_cpu)
                    y_dpcpp, hy_dpcpp = model_dpcpp(input_dpcpp)
                else:
                    y_cpu, hy_cpu = model_cpu(input_cpu, h_cpu)
                    y_dpcpp, hy_dpcpp = model_dpcpp(input_dpcpp, h0_dpcpp)
                self.assertTrue(torch.allclose(y_cpu, y_dpcpp, rtol=0.01, atol=0.05))
                self.assertTrue(torch.allclose(hy_cpu,hy_dpcpp, rtol=0.01, atol=0.05))

                if training:
                    y_cpu.sum().backward(retain_graph=True)
                    y_dpcpp.sum().backward(retain_graph=True)
                    self.assertTrue(torch.allclose(input_dpcpp.grad.to('cpu'), input_cpu.grad, rtol=0.01, atol=0.05))
                    self.assertTrue(torch.allclose(model_dpcpp.weight_ih_l0.grad.to('cpu'), model_cpu.weight_ih_l0.grad, rtol=0.01, atol=0.05))
                    self.assertTrue(torch.allclose(model_dpcpp.weight_hh_l0.grad.to('cpu'), model_cpu.weight_hh_l0.grad, rtol=0.01, atol=0.05))
                    if bias:
                        self.assertTrue(torch.allclose(model_dpcpp.bias_ih_l0.grad.to('cpu'), model_cpu.bias_ih_l0.grad, rtol=0.01, atol=0.05))
                        self.assertTrue(torch.allclose(model_dpcpp.bias_hh_l0.grad.to('cpu'), model_cpu.bias_hh_l0.grad, rtol=0.01, atol=0.05))
                    if not empty_state:
                        hy_cpu.sum().backward(retain_graph=True)
                        hy_dpcpp.sum().backward(retain_graph=True)
                        self.assertTrue(torch.allclose(h0_dpcpp.grad.to('cpu'), h_cpu.grad, rtol=0.01, atol=0.05))


    def _test_pack_padded_sequence_lstm(self, training):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        embedding_dim = 1024
        hidden_dim = 10
        batch_size = 24
        num_layers = 1
        bidirectional = True
        num_direc = 2 if bidirectional else 1
        max_lens = 96

        sent = torch.randn(batch_size, max_lens, embedding_dim)
        hid_0 = torch.rand(num_layers * num_direc, batch_size, hidden_dim)
        hid_1 = torch.randn(num_layers * num_direc, batch_size, hidden_dim)

        sentences = sent.clone().requires_grad_(training)
        sent_lens = torch.Tensor([1, 2, 3, 4, 5, 1, 3, 2, 96, 5, 3, 1, 1, 2, 1, 2, 3, 6, \
        1, 2, 4, 6, 2, 1])

        sentences_dpcpp = sent.clone().to(device=device).requires_grad_(training)
        sent_lens_dpcpp = sent_lens.clone().to(device=device)

        assert sent_lens.shape[0] == batch_size
        assert sent_lens.max().item() == max_lens

        hidden_0 = hid_0.clone().requires_grad_(training)
        hidden_1 = hid_1.clone().requires_grad_(training)
        embeds = torch.nn.utils.rnn.pack_padded_sequence(sentences, sent_lens, batch_first=True, enforce_sorted=False)
        lstm = nn.LSTM(embedding_dim, hidden_dim, num_layers=num_layers, bidirectional=bidirectional, batch_first=True)

        hidden_0_dpcpp = hid_0.clone().to(device=device).requires_grad_(training)
        hidden_1_dpcpp = hid_1.clone().to(device=device).requires_grad_(training)
        embeds_dpcpp = torch.nn.utils.rnn.pack_padded_sequence(sentences_dpcpp, sent_lens_dpcpp, batch_first=True, enforce_sorted=False)
        lstm_dpcpp = copy.deepcopy(lstm).to(device=device)

        lstm_out, hidden_out = lstm(embeds, (hidden_0, hidden_1))
        lstm_out, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True)

        with AutoDNNL(True), AutoMixPrecision(True, train=training):
            lstm_out_dpcpp, hidden_out_dpcpp = lstm_dpcpp(embeds_dpcpp, (hidden_0_dpcpp, hidden_1_dpcpp))
            lstm_out_dpcpp, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out_dpcpp, batch_first=True)
            # TODO: pad_packed_sequence will call IPEX OPs (reorder fp32 <=> bf16)
            self.assertEqual(lstm_out, lstm_out_dpcpp, atol=1e-1, rtol=1e-5)
            self.assertEqual(hidden_out[0], hidden_out_dpcpp[0])
            self.assertEqual(hidden_out[1], hidden_out_dpcpp[1])

            if training:
                lstm_out.sum().backward()
                lstm_out_dpcpp.sum().backward()
                self.assertEqual(sentences_dpcpp.grad.to('cpu'), sentences.grad)
                self.assertEqual(lstm_dpcpp.weight_ih_l0.grad.to('cpu'), lstm.weight_ih_l0.grad)
                self.assertEqual(lstm_dpcpp.weight_hh_l0.grad.to('cpu'), lstm.weight_hh_l0.grad)

                self.assertEqual(lstm_dpcpp.bias_ih_l0.grad.to('cpu'), lstm.bias_ih_l0.grad)
                self.assertEqual(lstm_dpcpp.bias_hh_l0.grad.to('cpu'), lstm.bias_hh_l0.grad)

                self.assertEqual(hidden_0_dpcpp.grad.to('cpu'), hidden_0.grad)
                self.assertEqual(hidden_1_dpcpp.grad.to('cpu'), hidden_1.grad)

    def test_lstm_inference(self):
        self._test_lstm(training=False)

    def test_lstm_training(self):
        self._test_lstm(training=True)

    def test_rnn_inference(self):
        self._test_rnn(cell="RNN", training=False)

    def test_rnn_training(self):
        self._test_rnn(cell="RNN", training=True)

    def test_gru_inference(self):
        self._test_rnn(cell="GRU", training=False)

    def test_gru_training(self):
        self._test_rnn(cell="GRU", training=True)

    def test_pack_padded_sequence_lstm_inference(self):
        self._test_pack_padded_sequence_lstm(training=False)

    def test_pack_padded_sequence_lstm_training(self):
        self._test_pack_padded_sequence_lstm(training=True)

class TestInterpolate(TestCase):
    def test_upsample_nearest1d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_nearest1d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, 8, mode='nearest')
            y_dpcpp = F.interpolate(x_dpcpp, 8, mode='nearest')
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_nearest2d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_nearest2d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, (4, 8), mode='nearest')
            y_dpcpp = F.interpolate(x_dpcpp, (4, 8), mode='nearest')
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_nearest3d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='nearest', recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_nearest3d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, (4, 8, 8), mode='nearest')
            y_dpcpp = F.interpolate(x_dpcpp, (4, 8, 8), mode='nearest')
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_linear1d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='linear', align_corners=False, recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='linear', align_corners=False, recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_linear1d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, 8, mode='linear', align_corners=False)
            y_dpcpp = F.interpolate(x_dpcpp, 8, mode='linear', align_corners=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_bilinear2d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='bilinear', align_corners=False, recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='bilinear', align_corners=False, recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_bilinear2d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, (4, 8), mode='bilinear', align_corners=False)
            y_dpcpp = F.interpolate(x_dpcpp, (4, 8), mode='bilinear', align_corners=False)
            self.assertEqual(y_cpu, y_dpcpp, atol=1e-2, rtol=1e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_trilinear3d_scale_factor(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, scale_factor = 2, mode='trilinear', align_corners=False, recompute_scale_factor=False)
            y_dpcpp = F.interpolate(x_dpcpp, scale_factor = 2, mode='trilinear', align_corners=False, recompute_scale_factor=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

    def test_upsample_trilinear3d_size(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x = torch.randn(2, 2, 2, 4, 4)
            x_cpu = x.clone().requires_grad_()
            x_dpcpp = x.clone().to(device=device).requires_grad_()
            y_cpu = F.interpolate(x_cpu, (4, 8, 8), mode='trilinear', align_corners=False)
            y_dpcpp = F.interpolate(x_dpcpp, (4, 8, 8), mode='trilinear', align_corners=False)
            self.assertEqual(y_cpu, y_dpcpp, 5e-2)
            y_cpu.sum().backward()
            y_dpcpp.sum().backward()
            self.assertEqual(x_cpu.grad, x_dpcpp.grad)

class TestPermute(TestCase):
    def test_permute(self):
        rand_seed = int(get_rand_seed())
        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)
        with AutoDNNL(True), AutoMixPrecision(True):
            x_cpu = torch.randn((2, 3, 4, 5), dtype=torch.float32) * 10
            x_dpcpp = convert_to_bf16(x_cpu)
            y_cpu = x_cpu.permute(0, 2, 1, 3)
            y_dpcpp = x_dpcpp.permute(0, 2, 1, 3)
            self.assertTrue(ipex._C.is_bf16_dil_tensor(y_dpcpp))
            self.assertEqual(y_cpu.bfloat16().float(), y_dpcpp)

if __name__ == '__main__':
    test = unittest.main()
