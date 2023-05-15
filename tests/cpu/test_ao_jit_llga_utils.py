import os
import copy
import tempfile
import torch
import torch.fx.experimental.optimization as optimization
from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
from functools import wraps
from torch.testing._internal.jit_utils import (
    JitTestCase,
    warmup_backward,
    get_execution_plan,
)
from torch.testing._internal.common_utils import (
    freeze_rng_state,
    run_tests,
    get_function_arglist,
    load_tests,
    TemporaryFileName,
)

from torch.jit._recursive import wrap_cpp_module

import intel_extension_for_pytorch as ipex

LLGA_FUSION_GROUP = "ipex::LlgaFusionGroup"

default_static_qconfig = QConfig(
    activation=MinMaxObserver.with_args(
        qscheme=torch.per_tensor_affine, dtype=torch.quint8
    ),
    weight=PerChannelMinMaxObserver.with_args(
        dtype=torch.qint8, qscheme=torch.per_channel_symmetric
    ),
)


def get_eltwise_fn(name):
    if hasattr(torch, name):
        return getattr(torch, name)
    elif hasattr(torch.nn.functional, name):
        return getattr(torch.nn.functional, name)
    else:
        if name == "hardswish_":
            return torch.nn.Hardswish(inplace=True)
        elif name == "mish_":
            return torch.nn.Mish(inplace=True)
        raise NameError("Eltwise function %s not found" % name)


# For fp32 and bf16 LLGA UT only
def llga_fp32_bf16_test_env(func):
    @wraps(func)
    def wrapTheFunction(*args):
        # make sure that the profiling mode is turned on
        torch._C._jit_set_profiling_mode(True)
        torch._C._jit_set_profiling_executor(True)

        ipex._C.set_llga_fp32_bf16_enabled(True)
        func(*args)
        ipex._C.set_llga_fp32_bf16_enabled(False)

    return wrapTheFunction


def all_backward_graphs(module):
    ge_state = module.get_debug_state()
    fwd_plan = get_execution_plan(ge_state)
    executors = fwd_plan.code.grad_executor_states()
    assert len(executors), "No backward graph found in the module"
    grad_executor = executors[0]
    bwd_plans = list(grad_executor.execution_plans.values())
    return [p.graph.copy() for p in bwd_plans]


def backward_graph(module):
    graphs = all_backward_graphs(module)
    assert len(graphs), "Warm up the module before calling backward_graph"
    return graphs[0]


def freeze(model):
    return wrap_cpp_module(torch._C._freeze_module(model._c, preserveParameters=True))


# port from pytorch/test/test_jit_fuser_te.py
def findFusionGroups(graph):
    result = []
    for n in graph.nodes():
        if n.kind() == LLGA_FUSION_GROUP:
            result.append(n.g("Subgraph"))
            continue
        for block in n.blocks():
            result += findFusionGroups(block)
    return result


def warmup_forward(f, *args, profiling_count=2):
    for i in range(profiling_count):
        results = f(*args)
    return results


class JitLlgaTestCase(JitTestCase):
    def checkScript(self, m, x, freeze=True):
        if isinstance(m, torch.nn.Module):
            m.eval()
        with torch.no_grad():
            ref = m(*x)
            scripted = torch.jit.script(m)
            if isinstance(scripted, torch.nn.Module) and freeze:
                scripted = torch.jit.freeze(scripted)
            warmup_forward(scripted, *x)
            graph = scripted.graph_for(*x)
            y = scripted(*x)
            self.assertEqual(y, ref)
        return graph, scripted

    def checkTrace(self, m, x, freeze=True, *args, **kwargs):
        if isinstance(m, torch.nn.Module):
            m.eval()
        with torch.no_grad(), torch._jit_internal._disable_emit_hooks():
            traced = torch.jit.trace(m, x)
            if isinstance(traced, torch.nn.Module) and freeze:
                traced = torch.jit.freeze(traced)
            warmup_forward(traced, *x)
            fwd_graph = traced.graph_for(*x)

            ref_o = m(*x)
            jit_o = traced(*x)
            self.assertEqual(jit_o, ref_o)
        return fwd_graph, traced

    def assertFused(self, graph, fused_patterns):
        for pat in fused_patterns:
            self.assertGraphContainsExactly(graph, pat, 0)

    def checkQuantizeTrace(
        self,
        model,
        x,
        atol=1e-3,
        rtol=1e-2,
        x_var=None,
        qconfig=default_static_qconfig,
        int8_bf16=False,
        freeze=True,
    ):
        graph, traced_model, fp32_model = self.prepareModel(
            model, x, qconfig, int8_bf16, freeze=freeze
        )
        with torch.no_grad():
            y = fp32_model(*x)
            y = y.to(torch.bfloat16) if int8_bf16 else y
            y_llga = traced_model(*x)
            self.assertEqual(y, y_llga, atol=atol, rtol=rtol)

            # test Fallback when input shape changes:
            if x_var:
                y_var = fp32_model(*x_var)
                y_var = y_var.to(torch.bfloat16) if int8_bf16 else y_var
                y_var_llga = traced_model(*x_var)
                self.assertEqual(y_var, y_var_llga, atol=atol, rtol=rtol)

            return graph

    def prepareModel(
        self,
        model,
        x,
        qconfig=default_static_qconfig,
        int8_bf16=False,
        prepare_inplace=True,
        convert_inplace=True,
        freeze=True,
    ):
        model.eval()
        fp32_model = copy.deepcopy(model)
        with torch.no_grad(), torch._jit_internal._disable_emit_hooks():
            ipex.nn.utils._model_convert.replace_dropout_with_identity(model)
            model = ipex.quantization.prepare(
                model, qconfig, x, inplace=prepare_inplace
            )
            # do calibration
            y = model(*x)
            # jit trace to insert quant/dequant
            if int8_bf16:
                with torch.cpu.amp.autocast():
                    convert_model = ipex.quantization.convert(
                        model, inplace=convert_inplace
                    )
                    traced_model = torch.jit.trace(convert_model, x)
            else:
                convert_model = ipex.quantization.convert(
                    model, inplace=convert_inplace
                )
                traced_model = torch.jit.trace(convert_model, x)
            if freeze:
                traced_model = torch.jit.freeze(traced_model)

            # warm up run
            y0 = traced_model(*x)
            # get the graph at the second run after freezing
            graph = traced_model.graph_for(*x)
            return graph, traced_model, fp32_model

    def checkPatterns(self, graph, patterns):
        fusion_groups = findFusionGroups(graph)
        assert len(fusion_groups) == len(
            patterns
        ), "length of subgraphs not equal to length of given patterns"

        for i in range(len(fusion_groups)):
            for pattern in patterns[i]:
                self.assertGraphContains(fusion_groups[i], pattern)

    def checkAttr(self, graph, node, attr):
        def count(block, node, attr):
            for n in block.nodes():
                if n.kind() == node:
                    self.assertFalse(n.hasAttribute("qtype"))
                for block in n.blocks():
                    count(block, node, attr)

        count(graph, node, attr)
