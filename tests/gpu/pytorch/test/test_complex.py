
import os
import sys
test_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
sys.path.append(test_root)

import common.xpu_test_base
# Owner(s): ["module: complex"]

import torch
from torch.testing._internal.common_device_type import instantiate_device_type_tests, dtypes
from torch.testing._internal.common_utils import TestCase, run_tests
from torch.testing._internal.common_dtype import complex_types

devices = (torch.device('cpu'), torch.device('cuda:0'))

class TestComplexTensor(TestCase):
    @dtypes(*complex_types())
    def test_to_list(self, device, dtype):
        # test that the complex float tensor has expected values and
        # there's no garbage value in the resultant list
        self.assertEqual(torch.zeros((2, 2), device=device, dtype=dtype).tolist(), [[0j, 0j], [0j, 0j]])

    @dtypes(torch.float32, torch.float64)
    def test_dtype_inference(self, device, dtype):
        # issue: https://github.com/pytorch/pytorch/issues/36834
        default_dtype = torch.get_default_dtype()
        torch.set_default_dtype(dtype)
        x = torch.tensor([3., 3. + 5.j], device=device)
        torch.set_default_dtype(default_dtype)
        self.assertEqual(x.dtype, torch.cdouble if dtype == torch.float64 else torch.cfloat)

instantiate_device_type_tests(TestComplexTensor, globals())

if __name__ == '__main__':
    run_tests()