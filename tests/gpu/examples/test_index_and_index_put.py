import torch
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch # noqa

import numpy as np

np.set_printoptions(threshold=np.inf)

cpu_device = torch.device("cpu")
dpcpp_device = torch.device("xpu")


class TestTorchMethod(TestCase):
    def test_index_and_index_put(self, dtype=torch.float):
        x_cpu = torch.randn([3, 3], dtype=torch.float, device=cpu_device)
        y_cpu = torch.randn([3, 3], dtype=torch.float, device=cpu_device)
        mask_cpu = y_cpu.gt(0)
        print("x_cpu:")
        print(x_cpu)
        print("mask_cpu:")
        print(mask_cpu)
        print("x_cpu[mask_cpu]:")
        print(x_cpu[mask_cpu])

        # dpcpp part
        x_dpcpp = x_cpu.to("xpu")
        mask_dpcpp = mask_cpu.to("xpu")
        print("mask index:")
        print(mask_dpcpp.to(cpu_device).nonzero())
        print("x_dpcpp[mask_dpcpp]:")
        print(x_dpcpp[mask_dpcpp].to("cpu"))
        self.assertEqual(mask_cpu.nonzero(),
                         mask_dpcpp.to(cpu_device).nonzero())
        self.assertEqual(x_cpu[mask_cpu], x_dpcpp[mask_dpcpp].to(cpu_device))

        # index put
        input = torch.ones([1], dtype=torch.float, device=cpu_device)
        indcies = torch.tensor([0, 0])
        x_cpu[indcies] = input
        print("index_put")
        print(x_cpu)
        x_cpu.index_put_([indcies], input, True)
        print("index_put accmulate=true")
        print(x_cpu)

        input = input.to("xpu")
        indcies = indcies.to("xpu")
        x_dpcpp[indcies] = input
        print("dpcpp index_put")
        print(x_dpcpp.cpu())
        x_dpcpp.index_put_([indcies], input, True)
        print("dpcpp  index_put accmulate=true")
        print(x_dpcpp.cpu())
        self.assertEqual(x_cpu, x_dpcpp.to(cpu_device))

    def test_index_put(self, dtype=torch.bfloat16):
        cpu_device = torch.device("cpu")
        dpcpp_device = torch.device("xpu")

        accumulate=True
        x_cpu = torch.zeros([4, 512, 128], dtype=dtype, device=cpu_device)
        y_cpu = torch.ones([4, 15000, 128], dtype=dtype, device=cpu_device)
        x_dpcpp = x_cpu.to(dpcpp_device)
        y_dpcpp = y_cpu.to(dpcpp_device)
        index_cpu = [torch.ones([4, 15000, 128], device=cpu_device).to(torch.long), torch.ones([4, 15000, 128], device=cpu_device).to(torch.long), torch.ones([4, 15000, 128], device=cpu_device).to(torch.long)]
        index_dpcpp = [torch.ones([4, 15000, 128], device=dpcpp_device).to(torch.long), torch.ones([4, 15000, 128], device=dpcpp_device).to(torch.long), torch.ones([4, 15000, 128], device=dpcpp_device).to(torch.long)]

        z_cpu = x_cpu.index_put_(index_cpu, y_cpu, accumulate)

        z_xpu = x_dpcpp.index_put_(index_dpcpp, y_dpcpp, accumulate)

        print("z_cpu = ", z_cpu)
        print("z_xpu = ", z_xpu.cpu())
        self.assertEqual(z_cpu, z_xpu.cpu())
