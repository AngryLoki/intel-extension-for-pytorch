import torch
import torch.nn.functional
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch  # noqa
import copy
import pytest


approximates = ["tanh", "none"]


class TestNNMethod(TestCase):

    def test_activation_relu(self, dtype=torch.float):
        relu_ = torch.nn.functional.relu_
        relu = torch.nn.functional.relu
        x_cpu = torch.tensor(
            [[-0.1, 0.2], [-0.2, 0.3], [0.4, 0.5], [0.5, -0.6]])
        x_dpcpp = x_cpu.to("xpu")

        relu_(x_cpu)
        relu_(x_dpcpp)
        self.assertEqual(x_cpu, x_dpcpp.cpu())

        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = relu(x_cpu)
        y_dpcpp = relu(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        y_cpu.backward(x_cpu)
        y_dpcpp.backward(x_dpcpp)

        self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_relu_block(self, dtype=torch.float):
        to_block_cpu = torch.nn.Conv2d(4, 4, kernel_size=3, padding=1)
        to_block_dpcpp = copy.deepcopy(to_block_cpu).xpu()
        test_shape = [1, 4, 3, 3]
        with torch.xpu.onednn_layout():
            relu_ = torch.nn.functional.relu_
            relu = torch.nn.functional.relu
            x_cpu = torch.randn(test_shape)
            x_dpcpp = x_cpu.to("xpu")
            relu_(to_block_cpu(x_cpu))
            relu_(to_block_dpcpp(x_dpcpp))

            self.assertEqual(x_cpu, x_dpcpp.cpu())
            x_cpu.requires_grad_(True)
            x_dpcpp.requires_grad_(True)
            y_cpu = relu(to_block_cpu(x_cpu))
            y_dpcpp = relu(to_block_dpcpp(x_dpcpp))

            self.assertEqual(y_cpu, y_dpcpp.cpu())
            y_cpu.backward(x_cpu)
            y_dpcpp.backward(x_dpcpp)

            self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_relu_channels_last(self, dtype=torch.float):
        x = torch.randn(1, 2, 3, 3, dtype=torch.float)
        w = torch.randn(2, 2, 3, 3, dtype=torch.float)
        conv = torch.nn.Conv2d(2, 2, kernel_size=3,
                               stride=1, padding=1, bias=False)
        bn = torch.nn.BatchNorm2d(2)
        relu = torch.nn.ReLU()
        conv.weight.data = w
        ref = conv(x)
        ref = bn(ref)
        ref = relu(ref)

        x = x.to("xpu").to(memory_format=torch.channels_last)
        w = w.to("xpu").to(memory_format=torch.channels_last)
        bn = bn.to("xpu")
        conv.weight.data = w
        real = conv(x)
        real = bn(real)
        real = relu(real)
        real = real.contiguous().cpu()

        self.assertEqual(real, ref)

    def test_activation_relu_channels_last_bwd(self, dtype=torch.float):
        relu = torch.nn.functional.relu
        x_cpu = torch.randn(1, 2, 3, 3, dtype=torch.float)
        grad_cpu = torch.randn(1, 2, 3, 3, dtype=torch.float)
        x_dpcpp = x_cpu.to("xpu").to(memory_format=torch.channels_last)
        grad_dpcpp = grad_cpu.to("xpu")

        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = relu(x_cpu)
        y_dpcpp = relu(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        y_cpu.backward(grad_cpu)
        y_dpcpp.backward(grad_dpcpp)

        self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_rrelu(self, dtype=torch.float):
        #  Will not check the result due to different random seeds on cpu and xpu
        RReLU = torch.nn.RReLU(0.1, 0.3)
        RReLU_dpcpp = copy.deepcopy(RReLU).to("xpu")
        x_cpu = torch.tensor(
            [[-0.1, 0.2], [-0.2, 0.3], [0.4, 0.5], [0.5, -0.6]])
        x_dpcpp = x_cpu.to("xpu")
        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = RReLU(x_cpu)
        y_dpcpp = RReLU_dpcpp(x_dpcpp)

        #  self.assertEqual(y_cpu, y_dpcpp.cpu())

        y_cpu.backward(x_cpu)
        y_dpcpp.backward(x_dpcpp)

        #  self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_gelu(self, dtype=torch.float):
        GELU = torch.nn.GELU()
        GELU_dpcpp = copy.deepcopy(GELU).to("xpu")
        x_cpu = torch.tensor(
            [[-0.1, 0.2], [-0.2, 0.3], [0.4, 0.5], [0.5, -0.6]])
        x_dpcpp = x_cpu.to("xpu")
        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = GELU(x_cpu)
        y_dpcpp = GELU_dpcpp(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        # y_cpu = torch.tensor([[1, 1],[1, 1],[1, 1],[1, 1]]);
        # y_dpcpp = y_cpu.to("xpu")
        y_cpu.backward(x_cpu)
        y_dpcpp.backward(x_dpcpp)

        self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_gelu_block(self, dtype=torch.float):
        to_block_cpu = torch.nn.Conv2d(4, 4, kernel_size=3, padding=1)
        to_block_dpcpp = copy.deepcopy(to_block_cpu).xpu()
        test_shape = [1, 4, 3, 3]
        with torch.xpu.onednn_layout():
            GELU = torch.nn.GELU()
            GELU_dpcpp = copy.deepcopy(GELU).to("xpu")
            x_cpu = torch.randn(test_shape)
            x_dpcpp = x_cpu.to("xpu")
            x_cpu.requires_grad_(True)
            x_dpcpp.requires_grad_(True)
            y_cpu = GELU(to_block_cpu(x_cpu))
            y_dpcpp = GELU_dpcpp(to_block_dpcpp(x_dpcpp))

            self.assertEqual(y_cpu, y_dpcpp.cpu())
            y_cpu.backward(x_cpu)
            y_dpcpp.backward(x_dpcpp)

            self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_prelu(self, dtype=torch.float):
        PReLU = torch.nn.PReLU(num_parameters=1)
        PReLU_dpcpp = copy.deepcopy(PReLU).to("xpu")
        x_cpu = torch.tensor(
            [[-0.1, 0.2], [-0.2, 0.3], [0.4, 0.5], [0.5, -0.6]])
        x_dpcpp = x_cpu.to("xpu")
        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = PReLU(x_cpu)
        y_dpcpp = PReLU_dpcpp(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        y_cpu.backward(x_cpu)
        y_dpcpp.backward(x_dpcpp)

        self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

        # 2-dim
        PReLU = torch.nn.PReLU(num_parameters=3)
        PReLU_dpcpp = copy.deepcopy(PReLU).to("xpu")
        input_2_dim_cpu = torch.rand(2, 3)
        input_2_dim_xpu = input_2_dim_cpu.xpu()
        y_cpu = PReLU(input_2_dim_cpu)
        y_xpu = PReLU_dpcpp(input_2_dim_xpu)
        self.assertEqual(y_cpu, y_xpu.cpu())

        # 4-dim
        PReLU = torch.nn.PReLU(num_parameters=2)
        PReLU_dpcpp = copy.deepcopy(PReLU).to("xpu")
        input_4_dim_cpu = torch.rand(1, 2, 3, 4)
        input_4_dim_xpu = input_4_dim_cpu.xpu()
        y_cpu = PReLU(input_4_dim_cpu)
        y_xpu = PReLU_dpcpp(input_4_dim_xpu)
        self.assertEqual(y_cpu, y_xpu.cpu())

    def test_activation_prelu_multi_weight(self, dtype=torch.float):
        PReLU = torch.nn.PReLU(num_parameters=3)
        PReLU_dpcpp = copy.deepcopy(PReLU).to("xpu")
        x_cpu = torch.tensor(
            [[-0.1, 0.2, 2], [-0.2, 0.3, 2], [0.4, 0.5, 2], [0.5, -0.6, 3]])
        x_dpcpp = x_cpu.to("xpu")
        x_cpu.requires_grad_(True)
        x_dpcpp.requires_grad_(True)
        y_cpu = PReLU(x_cpu)
        y_dpcpp = PReLU_dpcpp(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        y_cpu.backward(x_cpu)
        y_dpcpp.backward(x_dpcpp)

        self.assertEqual(x_cpu.grad, x_dpcpp.grad.cpu())

    def test_activation_mish(self, dtype=torch.float):
        test_shape = [1, 4, 3, 3]
        Mish = torch.nn.Mish()
        Mish_dpcpp = copy.deepcopy(Mish).to("xpu")
        x_cpu = torch.randn(test_shape)
        x_dpcpp = x_cpu.to("xpu")
        y_cpu = Mish(x_cpu)
        y_dpcpp = Mish_dpcpp(x_dpcpp)

        self.assertEqual(y_cpu, y_dpcpp.cpu())

        Mish = torch.nn.Mish(inplace=True)
        Mish_dpcpp = copy.deepcopy(Mish).to("xpu")
        Mish(x_cpu)
        Mish_dpcpp(x_dpcpp)
        self.assertEqual(x_cpu, x_dpcpp.cpu())
