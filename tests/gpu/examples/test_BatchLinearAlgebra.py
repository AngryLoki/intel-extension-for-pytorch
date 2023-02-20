import itertools
import time

import torch
from torch.testing._internal.common_utils import TestCase

import intel_extension_for_pytorch # noqa

import pytest

cpu_device = torch.device("cpu")
dpcpp_device = torch.device("xpu")


class TestTorchMethod(TestCase):
    def test_batch_linear_algebra(self, dtype=torch.float):
        x_cpu = torch.randn(5, 5)

        x_dpcpp = x_cpu.to(dpcpp_device)
        # y_cpu1 = x_cpu.new_ones((2, 3))
        y_cpu1 = torch.randn(5, 5)
        # y_cpu2 = x_cpu.new_ones((2, 3))
        y_cpu2 = torch.randn(5, 5)

        y_dpcpp1 = y_cpu1.to(dpcpp_device)
        y_dpcpp2 = y_cpu2.to(dpcpp_device)

        print("y_cpu", torch.tril(y_cpu2))
        print("y_dpcpp", torch.tril(y_dpcpp2).to("cpu"))
        self.assertEqual(torch.tril(y_cpu2),
                         torch.tril(y_dpcpp2).to(cpu_device))

        print("y_cpu", torch.triu(y_cpu2))
        print("y_dpcpp", torch.triu(y_dpcpp2).to("cpu"))
        self.assertEqual(torch.triu(y_cpu2),
                         torch.triu(y_dpcpp2).to(cpu_device))

    @pytest.mark.skipif("not torch.xpu.has_onemkl()")
    def test_cholesky(self, dtype=torch.float):
        x_cpu = torch.randn(3, 3)
        print("x cpu \n", x_cpu)
        x_cpu = torch.mm(x_cpu, x_cpu.t())
        print("x mm cpu \n", x_cpu)

        x_dpcpp = x_cpu.to(dpcpp_device)
        print(" xpu x_dpcpp \n", x_dpcpp.cpu())

        res = torch.cholesky(x_cpu)
        print("res cpu \n", res)
        res_dpcpp = torch.cholesky(x_dpcpp)
        print("res_dpcpp  \n", res_dpcpp.cpu())
        res_tensor_dpcpp = x_dpcpp.cholesky()
        print("res_tensor_dpcpp  \n", res_tensor_dpcpp.cpu())
        self.assertEqual(res.to(cpu_device), res_dpcpp.to(cpu_device))
        self.assertEqual(res.to(cpu_device), res_tensor_dpcpp.to(cpu_device))

    @pytest.mark.skipif("not torch.xpu.has_onemkl()")
    def test_cholesky_solve(self, dtype=torch.float):
        a = torch.randn([3, 3], device=cpu_device)
        print(" cpu a  ", a)
        a = torch.mm(a, a.t())
        print(" cpu mm a  ", a)
        a_dpcpp = a.to(dpcpp_device)
        print(" xpu a_dpcpp  ", a_dpcpp.cpu())

        u = torch.cholesky(a)
        print(" =cpu u ==", u)
        u_dpcpp = u.to(dpcpp_device)
        print(" xpu u_dpcpp  ", u_dpcpp.cpu())

        b = torch.randn([3, 2], device=cpu_device)
        print(" cpu b  ", b)
        b_dpcpp = b.to(dpcpp_device)
        print(" xpu b_dpcpp  ", b_dpcpp.cpu())

        res = torch.cholesky_solve(b, u)
        print(" cpu res  ", res)
        check_res = torch.mm(a.inverse(), b)
        print(" cpu check_res  ", check_res)
        res_tensor = b.cholesky_solve(u)
        print(" cpu res_tensor  ", res_tensor)

        res_dpcpp = torch.cholesky_solve(b_dpcpp, u_dpcpp)
        print(" xpu res_dpcpp  ", res_dpcpp.cpu())

        check_res_dpcpp = torch.mm(a_dpcpp.inverse(), b_dpcpp)
        print(" xpu check_res_dpcpp  ", check_res_dpcpp.cpu())

        res_tensor_dpcpp = b_dpcpp.cholesky_solve(u_dpcpp)
        print(" xpu res_tensor_dpcpp  ", res_tensor_dpcpp.cpu())

        self.assertEqual(res.to(cpu_device), res_dpcpp.to(cpu_device))
        self.assertEqual(check_res.to(cpu_device), check_res_dpcpp.to(cpu_device))
        self.assertEqual(res_tensor.to(cpu_device), res_tensor_dpcpp.to(cpu_device))

    @pytest.mark.skipif("not torch.xpu.has_onemkl()")
    def test_logdet(self, dtype=torch.float):
        ts = int(time.time())
        torch.manual_seed(ts)

        A = torch.randn(3, 3).to(cpu_device)

        a = torch.det(A)
        print("torch.det(A)", a.to(cpu_device))
        b = torch.logdet(A)
        print("torch.logdet(A)", b.to(cpu_device))

        print("A", A.to(cpu_device))
        A_det = A.det()
        print("A.det()", A_det.to(cpu_device))
        A_det_log = A.det().log()
        print("A.det().log()", A_det_log.to(cpu_device))

        ######
        A_dpcpp = A.to(dpcpp_device)

        a_dpcpp = torch.det(A_dpcpp)
        print("torch.det(A_dpcpp)", a_dpcpp.to(cpu_device))
        b_dpcpp = torch.logdet(A_dpcpp)
        print("torch.logdet(A_dpcpp)", b_dpcpp.to(cpu_device))

        print("A_dpcpp", A_dpcpp.to(cpu_device))
        A_dpcpp_det = A_dpcpp.det()
        print("A_dpcpp.det()", A_dpcpp_det.to(cpu_device))
        A_dpcpp_det_log = A_dpcpp.det().log()
        print("A_dpcpp.det().log()", A_dpcpp_det_log.to(cpu_device))

        # asssert
        self.assertEqual(a, a_dpcpp.to(cpu_device))
        self.assertEqual(b, b_dpcpp.to(cpu_device))
        self.assertEqual(A.to(cpu_device), A_dpcpp.to(cpu_device))
        self.assertEqual(A_det.to(cpu_device), A_dpcpp_det.to(cpu_device))
        self.assertEqual(A_det_log.to(cpu_device), A_dpcpp_det_log.to(cpu_device))

    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="not torch.xpu.has_onemkl()")
    def test_lu_out(self, dtype=torch.float):
        for size in [(3, 3), (5, 5), (10, 10)]:
            A = torch.rand(size)
            P, L, U = torch.linalg.lu(A)
            A_xpu = A.to('xpu')
            P_xpu, L_xpu, U_xpu = torch.linalg.lu(A_xpu)
            self.assertEqual(P_xpu.to(cpu_device), P)
            self.assertEqual(L_xpu.to(cpu_device), L)
            self.assertEqual(U_xpu.to(cpu_device), U)


    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="not torch.xpu.has_onemkl()")
    def test_lu(self, dtype=torch.float):
        def _validate(A, LU, pivot):
            P, L, U = torch.lu_unpack(LU, pivot)
            A_ = torch.matmul(P, torch.matmul(L, U))
            self.assertEqual(A, A_)

        for size in [(3, 3), (2, 3, 3), (128, 64, 64)]:
            A = torch.randn(size, dtype=dtype)

            # CPU
            A_cpu = A.to('cpu')
            LU_cpu, pivot_cpu = torch.lu(A_cpu)
            _validate(A_cpu, LU_cpu, pivot_cpu)
            LU_cpu, pivot_cpu = A_cpu.lu()
            _validate(A_cpu, LU_cpu, pivot_cpu)

            # XPU
            A_xpu = A.to('xpu')
            LU_xpu, pivot_xpu = torch.lu(A_xpu)
            _validate(A_xpu.cpu(), LU_xpu.cpu(), pivot_xpu.cpu())
            LU_xpu, pivot_xpu = A_xpu.lu()
            _validate(A_xpu.cpu(), LU_xpu.cpu(), pivot_xpu.cpu())

    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="not torch.xpu.has_onemkl()")
    def test_lu_solve(self, dtype=torch.float):
        def _validate(A, x, b):
            b_ = torch.matmul(A, x)
            self.assertEqual(b, b_, rtol=1.3e-6, atol=0.02)

        for sizeA, sizeb in [[(3, 3), (3, 1)],
                             [(2, 3, 3), (2, 3, 1)],
                             [(2, 3, 3), (2, 3, 5)],
                             [(128, 64, 64), (128, 64, 4)]]:
            A = torch.randn(sizeA, dtype=dtype)
            b = torch.randn(sizeb, dtype=dtype)

            # CPU
            A_cpu = A.to('cpu')
            b_cpu = b.to('cpu')
            x_cpu = torch.lu_solve(b_cpu, *A_cpu.lu())
            _validate(A_cpu, x_cpu, b_cpu)
            x_cpu = b_cpu.lu_solve(*A_cpu.lu())
            _validate(A_cpu, x_cpu, b_cpu)

            # XPU
            A_xpu = A.to('xpu')
            b_xpu = b.to('xpu')
            x_xpu = torch.lu_solve(b_xpu, *A_xpu.lu())
            _validate(A_xpu.cpu(), x_xpu.cpu(), b_xpu.cpu())
            x_xpu = b_xpu.lu_solve(*A_xpu.lu())
            _validate(A_xpu.cpu(), x_xpu.cpu(), b_xpu.cpu())

    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="not torch.xpu.has_onemkl()")
    def test_solve(self, dtype=torch.float):
        def _validate(A, x, b):
            d_ = torch.dist(b, torch.matmul(A, x))
            d = torch.zeros_like(d_)
            self.assertEqual(d, d_, rtol=1.3e-6, atol=5e-5)

        for sizeA, sizeb in [[(3, 3), (3, 1)],
                             [(2, 3, 3), (2, 3, 1)],
                             [(2, 3, 3), (2, 3, 5)],
                             [(2, 3, 1, 4, 4), (2, 3, 1, 4, 6)]]:
            A = torch.randn(sizeA, dtype=dtype)
            b = torch.randn(sizeb, dtype=dtype)

            # CPU
            A_cpu = A.to('cpu')
            b_cpu = b.to('cpu')
            x_cpu = torch.linalg.solve(A_cpu, b_cpu)
            _validate(A_cpu, x_cpu, b_cpu)

            # XPU
            A_xpu = A.to('xpu')
            b_xpu = b.to('xpu')
            x_xpu = torch.linalg.solve(A_xpu, b_xpu)
            _validate(A_xpu.cpu(), x_xpu.cpu(), b_xpu.cpu())

    @pytest.mark.skipif("not torch.xpu.has_onemkl()")
    def test_inverse(self, dtype=torch.float):
        def _validate(A, A_):
            self.assertEqual(torch.matmul(A, A_), torch.eye(A.size(-1)).expand_as(A),
                             rtol=1.3e-6, atol=0.005)

        for size in [(3, 3), (2, 3, 3), (128, 64, 64)]:
            A = torch.randn(size, dtype=dtype)

            # CPU
            A_cpu = A.to('cpu')
            Ai_cpu = torch.inverse(A_cpu)
            _validate(A_cpu, Ai_cpu)
            Ai_cpu = A_cpu.inverse()
            _validate(A_cpu, Ai_cpu)

            # XPU
            A_xpu = A.to('xpu')
            Ai_xpu = torch.inverse(A_xpu)
            _validate(A_xpu.cpu(), Ai_xpu.cpu())
            Ai_xpu = A_xpu.inverse()
            _validate(A_xpu.cpu(), Ai_xpu.cpu())

    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="onemkl not compiled for IPEX")
    def test_qr(self, dtype=torch.float):
        def _validate(A, Q, R):
            if Q.size(0) != 0:
                self.assertEqual((Q @ R).cpu(), A.cpu())
                q_size = list(Q.size())
                q_size[-2] = q_size[-1]
                diag_tensor = torch.eye(q_size[-1]).expand(q_size)
                valid_tensor = (Q.transpose(-2, -1) @ Q).round()
                self.assertEqual(valid_tensor.cpu(), diag_tensor.cpu())

        for size in [(3, 3), (2, 5, 3), (2, 3, 5), (128, 64, 64)]:
            for mode in ["reduced", "r", "complete"]:
                A = torch.randn(size, dtype=dtype)

                print("mode is ", mode)
                # CPU
                A_cpu = A.to('cpu')
                q_cpu, r_cpu = torch.linalg.qr(A_cpu, mode=mode)
                print("ON CPU:")
                print("A = ", A_cpu)
                print("Q = ", q_cpu)
                print("R = ", r_cpu)
                _validate(A_cpu, q_cpu, r_cpu)

                # XPU
                A_xpu = A.to('xpu')
                q_xpu, r_xpu = torch.linalg.qr(A_xpu, mode=mode)
                print("ON XPU:")
                print("A = ", A_xpu.cpu())
                print("Q = ", q_xpu.cpu())
                print("R = ", r_xpu.cpu())
                _validate(A_xpu, q_xpu, r_xpu)

                self.assertEqual(r_cpu, r_xpu, rtol=1.3e-6, atol=5e-5)

    @pytest.mark.skipif(not torch.xpu.has_onemkl(), reason="onemkl not compiled for IPEX")
    def test_ormqr(self, dtype=torch.float):
        A = torch.randn(8, 5)
        c = torch.randn(8, 8)

        for left, transpose in itertools.product([True, False], [True, False]):
            print("torch.ormqr:")
            # ON CPU
            A_cpu = A.to('cpu')
            c_cpu = c.to('cpu')
            a_cpu, tau_cpu = torch.geqrf(A_cpu)
            c__cpu = torch.ormqr(a_cpu, tau_cpu, c_cpu, left, transpose)
            print("cpu: c_ = ", c__cpu.cpu())
            # ON GPU
            A_xpu = A.to('xpu')
            c_xpu = c.to('xpu')
            a_xpu, tau_xpu = torch.geqrf(A_xpu)
            c__xpu = torch.ormqr(a_xpu, tau_xpu, c_xpu, left, transpose)
            print("xpu: c_ = ", c__xpu.cpu())
            # validate
            self.assertEqual(c__cpu, c__xpu)

            print("torch.tensor.ormqr:")
            # ON CPU
            A_cpu = A.to('cpu')
            c_cpu = c.to('cpu')
            a_cpu, tau_cpu = A_cpu.geqrf()
            c__cpu = a_cpu.ormqr(tau_cpu, c_cpu, left, transpose)
            print("cpu: c_ = ", c__cpu)
            # ON XPU
            A_xpu = A.to('xpu')
            c_xpu = c.to('xpu')
            a_xpu, tau_xpu = A_xpu.geqrf()
            c__xpu = a_xpu.ormqr(tau_xpu, c_xpu, left, transpose)
            print("xpu: c_ = ", c__xpu.cpu())
            # validate
            self.assertEqual(c__cpu, c__xpu)
