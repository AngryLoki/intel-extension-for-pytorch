import torch
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch  # noqa

cpu_device = torch.device("cpu")
dpcpp_device = torch.device("xpu")


class TestTorchMethod(TestCase):
    def test_eq(self, dtype=torch.float):
        x1 = torch.tensor([[1.0, 2.0], [3.0, 4.0]]).to("xpu")
        x2 = torch.tensor([[1.0, 1.0], [4.0, 4.0]]).to("xpu")

        self.assertEqual(False, torch.equal(x1.cpu(), x2.cpu()))
        self.assertEqual(True, torch.equal(x1.cpu(), x1.cpu()))
        self.assertEqual(True, torch.equal(x2.cpu(), x2.cpu()))
