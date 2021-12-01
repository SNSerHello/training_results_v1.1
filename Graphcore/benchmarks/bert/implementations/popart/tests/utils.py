# Copyright (c) 2019 Graphcore Ltd. All rights reserved.

import math
import os
import json
import tempfile
import pytest
import numpy as np

from typing import Iterable, Tuple, Any, Union, Mapping, Callable, Optional
from itertools import chain
from functools import reduce

import popart

import onnx
from onnx import numpy_helper

import torch
import torch.nn as nn


def requires_remote_buffers(*args):
    return pytest.param(*args, marks=pytest.mark.requires_remote_buffers)


class TestFailureError(Exception):
    __test__ = False


def getTensorError(tA, pA):
    # pA, tA are corresponding tensors from two models
    pA_shape = np.shape(pA)
    tA_shape = np.shape(tA)
    assert (pA_shape == tA_shape), "Arrays must be same shape"

    tA = tA.astype(np.float32)
    pA = pA.astype(np.float32)
    ss_err = np.sum((np.array(pA) - np.array(tA))**2)
    ss_pA = np.sum(np.array(pA)**2)
    ss_tA = np.sum(np.array(tA)**2)
    return ss_err / (math.sqrt(ss_pA * ss_tA) + 1.0e-8)


def reportTensorError(result):
    reportStr = " |pA - tA|^2 / (|pA||tA| + 1e-8)  = " + str(result) + "\n"
    return reportStr


def checkResult(result, margin):
    if np.isnan(result):
        raise TestFailureError(str(result) + " is NaN")
    elif (result > margin):
        print(reportTensorError(result))
        raise TestFailureError(str(result) + " is greater than " + str(margin))


def check_tensor(A, B, margin=1.5e-8):
    result = getTensorError(A, B)
    checkResult(result, margin)


def make_tuple(something: Any) -> Tuple:
    if isinstance(something, tuple) or isinstance(something, list):
        def concat(accl: Iterable, s: Any) -> Iterable:
            return chain(accl, make_tuple(s))

        return tuple(reduce(concat, something, ()))
    return (something, )


def run_py(proto: onnx.ModelProto,
           data: Mapping[str, np.ndarray],
           outputs: Optional[Union[str, Iterable[str]]],
           loss: Optional[str] = None,
           optimizer: Optional[popart.Optimizer] = None,
           patterns: Optional[popart.Patterns] = None,
           return_stats: bool = False,
           log_dir: Optional[str] = None,
           ipus: Optional[int] = None,
           batches_per_step: int = 1,
           user_options: Optional[Mapping[str, Any]] = None,
           skip_execution: bool = False,
           execution_mode: str = 'DEFAULT',
           replication_factor: int = 1,
           replicated_weight_sharding: bool = False,
           num_reps: int = 1):
    outputs = make_tuple(outputs)

    # Setting up the Session
    data_flow = popart.DataFlow(
        batches_per_step, {output: popart.AnchorReturnType("ALL")
                           for output in outputs})

    if user_options is None:
        user_options = {}
    options = popart.SessionOptions()
    options.reportOptions = {"showVarStorage": "true"}
    if replicated_weight_sharding:
        options.weightTensorLocationSettings.location.replicatedTensorSharding.On
        options.optimizerStateTensorLocationSettings.location.replicatedTensorSharding.On
    if replication_factor > 1:
        options.enableReplicatedGraphs = True
        options.replicatedGraphCount = replication_factor
    if execution_mode == 'PHASED':
        options.enableOutlining = True
        options.outlineThreshold = -np.inf
        options.enableOutliningCopyCostPruning = False
        options.autoRecomputation = popart.RecomputationType.Standard
        options.virtualGraphMode = popart.VirtualGraphMode.ExecutionPhases
        options.explicitRecomputation = True
        options.aliasZeroCopy = True
        options.batchSerializationSettings.factor = user_options[
            "batchSerializationFactor"]
        options.executionPhaseSettings.phases = user_options["executionPhases"]
        ipus = 2
    else:
        options.enableGroupedMatmuls = False
        options.enableStochasticRounding = False
        options.constantWeights = True
        options.outlineThreshold = 10.0
        if ipus is not None and ipus > 1:
            options.virtualGraphMode = popart.VirtualGraphMode.Manual
        else:
            ipus = 1

    for key, value in user_options.items():
        if key not in ["batchSerializationFactor", "executionPhases"]:
            setattr(options, key, value)

    if return_stats:
        options.engineOptions = {
            "debug.allowOutOfMemory": "true",
            "debug.instrument": "true",
            "opt.internalExchangeOptimisationTarget": "balanced",
        }

    request_ipus = pow(2, math.ceil(math.log2(ipus)))
    request_ipus *= replication_factor
    dm = popart.DeviceManager()
    dm.setOnDemandAttachTimeout(int(1e4))
    device = dm.acquireAvailableDevice(
        request_ipus,
        connectionType=popart.DeviceConnectionType.OnDemand,
        selectionCriterion=popart.DeviceSelectionCriterion.Random)
    if device is None:
        raise Exception("Failed to acquire IPU.")

    print("Compiling graph")
    if optimizer is not None:
        session = popart.TrainingSession(fnModel=proto,
                                         deviceInfo=device,
                                         dataFlow=data_flow,
                                         userOptions=options,
                                         loss=loss,
                                         optimizer=optimizer,
                                         patterns=patterns)
    else:
        session = popart.InferenceSession(fnModel=proto,
                                          deviceInfo=device,
                                          dataFlow=data_flow,
                                          userOptions=options,
                                          patterns=patterns)

    if skip_execution:
        device.detach()
        return session

    # Compile the Poplar Graph. If it fails, return the memory stats
    try:
        session.prepareDevice()
    except popart.session.OutOfMemoryException as e:
        if return_stats and log_dir:
            import gcprofile
            os.makedirs(log_dir, exist_ok=True)
            gcprofile.save_popart_report(session,
                                         log_dir=log_dir,
                                         exception=e)
        device.detach()
        raise e
    print("Compilation complete")

    session.weightsFromHost()
    session.setRandomSeed(1984)

    anchors = session.initAnchorArrays()

    rf = user_options.get("replicatedGraphCount")
    if rf is not None and rf > 1:
        data = {k: np.repeat(v[np.newaxis], rf, 0)
                for k, v in data.items()}

    # Add a gradient accumulation factor dimension if needed
    af = user_options.get("accumulationFactor")
    if af is not None and af > 1:
        data = {k: np.repeat(v[np.newaxis], af, 0)
                for k, v in data.items()}

    # Add a batches_per_step dimension if needed
    if batches_per_step > 1:
        data = {k: np.repeat(v[np.newaxis], batches_per_step, 0)
                for k, v in data.items()}

    for _ in range(num_reps):
        stepio = popart.PyStepIO(data, anchors)
        session.run(stepio)

    with tempfile.TemporaryDirectory() as tmp:
        file_path = os.path.join(tmp, "model.onnx")
        session.modelToHost(file_path)
        post_proto = onnx.load(file_path)

    # Release device
    device.detach()

    if return_stats:
        if log_dir:
            import gcprofile
            os.makedirs(log_dir, exist_ok=True)
            reports = gcprofile.save_popart_report(session, log_dir=log_dir)
            graph_report = json.loads(reports["graph"])
            exec_report = json.loads(reports["execution"])
        else:
            graph_report = json.loads(session.getGraphReport())
            exec_report = json.loads(session.getExecutionReport())
        max_tile_memory = max(graph_report["memory"]["byTile"]["total"])
        total_memory = np.sum(graph_report["memory"]["byTile"]["total"])
        cycles = exec_report["simulation"]["cycles"]
        return (anchors[output] for output in outputs
                ), post_proto, total_memory, max_tile_memory, cycles
    return (anchors[output] for output in outputs), post_proto


def onnx_to_numpy(tensor: onnx.TensorProto) -> np.ndarray:
    if tensor.data_type == onnx.TensorProto.FLOAT16:
        int_data = np.asarray(tensor.int32_data, np.int32)
        np_tensor = int_data.view(dtype=np.float16).reshape(tensor.dims)
    else:
        np_tensor = numpy_helper.to_array(tensor)
    return np_tensor


def copy_weights_to_onnx(
        target_model: onnx.ModelProto, source_model: onnx.ModelProto,
        onnx_to_onnx: Mapping[str, str],
        transform: Mapping[str, Callable[[np.ndarray], np.ndarray]]):
    onnx_weights = {}
    for weight in source_model.graph.initializer:
        onnx_weights[weight.name] = onnx_to_numpy(weight)

    if len(onnx_weights) > 0:
        for weight in target_model.graph.initializer:
            name = onnx_to_onnx.get(weight.name, weight.name)
            if name in onnx_weights.keys():
                if name in transform.keys():
                    src_weight = transform[name](onnx_weights[name])
                    new_tensor = numpy_helper.from_array(src_weight)
                else:
                    new_tensor = numpy_helper.from_array(onnx_weights[name])
                weight.MergeFrom(new_tensor)
    return target_model


def extract_initializers(
        onnx_model: onnx.ModelProto,
        onnx_to_onnx: Mapping[str, str] = {},
        transform: Mapping[str, Callable[[np.ndarray], np.ndarray]] = {}):
    initializers = {}
    for weight in onnx_model.graph.initializer:
        name = onnx_to_onnx.get(weight.name, weight.name)
        np_weight = onnx_to_numpy(weight)
        if name in transform.keys():
            np_weight = transform[name](np_weight)
        initializers[name] = np_weight
    return initializers


def copy_weights_to_torch(
        torch_model: nn.Module, onnx_model: onnx.ModelProto,
        torch_to_onnx: Mapping[str, str],
        transform: Mapping[str, Callable[[np.ndarray], np.ndarray]]):
    onnx_weights = {}
    w = [weight.name for weight in onnx_model.graph.initializer]
    for weight in onnx_model.graph.initializer:
        onnx_weights[weight.name] = onnx_to_numpy(weight)

    if len(onnx_weights) > 0:
        for name, w in torch_model.named_parameters():
            if name in torch_to_onnx.keys():
                onnx_name = torch_to_onnx[name]
                if isinstance(onnx_name, list):
                    # To handle serialized layers where weights are split into multiple tensors for serialization. thse need to be concatenated and copied as a single tensor
                    weights = []
                    for phased_split_name in onnx_name:
                        if phased_split_name in onnx_weights.keys():
                            phased_split = onnx_weights[phased_split_name]
                            weights.append(phased_split)
                        else:
                            ''' error split not found in initializers '''
                            print("Error : "+phased_split_name+" not found")
                    if name in transform.keys():
                        onnx_tensor = torch.Tensor(transform[name](
                            weights))
                        # PyTorch CPU does not support float16...
                        w.data.copy_(onnx_tensor.float())
                    else:
                        # transformer required to concatenate list of weights split for embedding serialization
                        print("Error : transformer required for concatenating weights from embedding serialization")
                else:
                    if onnx_name in onnx_weights.keys():
                        if name in transform.keys():
                            onnx_tensor = torch.Tensor(transform[name](
                                onnx_weights[onnx_name]))
                        else:
                            onnx_tensor = torch.Tensor(onnx_weights[onnx_name])
                        # PyTorch CPU does not support float16...
                        w.data.copy_(onnx_tensor.float())


def check_tensors(torch_outputs: Iterable[np.ndarray],
                  onnx_outputs: Iterable[np.ndarray],
                  margin: float = 1.5e-8):
    for t_torch, t_onnx in zip(torch_outputs, onnx_outputs):
        check_oom_failures(t_torch, t_onnx)
        check_tensor(t_onnx.reshape(t_torch.shape), t_torch, margin=margin)


def check_oom_failures(torch_output: np.ndarray, onnx_output: np.ndarray):
    failed_methods = []
    # Produce an error indicating which implementation ran out of memory during
    # compilation. Both could fail, so we won't print exclusively.
    if type(torch_output) == float and np.isnan(torch_output):
        failed_methods.append("Custom Operation")

    if type(onnx_output) == float and np.isnan(onnx_output):
        failed_methods.append("ONNX")

    if len(failed_methods) > 0:
        msg = "OOM in the following implementations: " + \
            ", ".join(failed_methods)

        raise TestFailureError(msg)


def check_onnx_model(
        model_1: onnx.ModelProto,
        model_2: onnx.ModelProto,
        onnx_to_onnx: Mapping[str, str] = {},
        transform: Mapping[str, Callable[[np.ndarray], np.ndarray]] = {},
        allow_missing: bool = True):
    model_1_weights = {}
    for weight in model_1.graph.initializer:
        model_1_weights[weight.name] = onnx_to_numpy(weight)

    if len(model_1_weights) > 0:
        for w_2 in model_2.graph.initializer:
            name = onnx_to_onnx.get(w_2.name, w_2.name)
            if name in model_1_weights.keys():
                np_w_1 = model_1_weights[name]
                if name in transform.keys():
                    np_w_1 = transform[name](np_w_1)
                elif w_2.name in transform.keys():
                    np_w_1 = transform[w_2.name](np_w_1)
                np_w_2 = onnx_to_numpy(w_2)
                try:
                    check_tensor(np_w_1, np_w_2)
                except TestFailureError as e:
                    print(f"For weight: (1) {name}, (2) {w_2.name}")
                    raise e
            else:
                if not allow_missing:
                    raise TestFailureError(f"Missing weight mapping for model_2 weight {name}")


def check_model(torch_model: nn.Module,
                onnx_model: onnx.ModelProto,
                torch_to_onnx: Mapping[str, str],
                transform: Mapping[str, Callable[[np.ndarray], np.ndarray]],
                margin: float = 1.5e-8):
    onnx_weights = {}
    for weight in onnx_model.graph.initializer:
        onnx_weights[weight.name] = onnx_to_numpy(weight)

    if len(onnx_weights) > 0:
        # Iterating the parameters reversed checks the model from the loss backwards,
        # which helps with debugging
        issue = None
        for name, w in reversed(list(torch_model.named_parameters())):
            if name in torch_to_onnx.keys():
                onnx_name = torch_to_onnx[name]
                if isinstance(onnx_name, list):
                    # To handle serialized layers where weights are split into multiple tensors for serialization. thse need to be concatenated and copied as a single tensor
                    weights = []
                    for phased_split_name in onnx_name:
                        if phased_split_name in onnx_weights.keys():
                            phased_split = onnx_weights[phased_split_name]
                            weights.append(phased_split)
                        else:
                            ''' error split not found in initializers '''
                            print("Error : "+phased_split_name+" not found")
                    if name in transform.keys():
                        onnx_w = transform[name](weights)
                    else:
                        # Transformer required to concatenate list of weights split for embedding serialization
                        print("Error : transformer required for concatenating weights from embedding serialization")
                else:
                    if name in transform.keys():
                        onnx_w = transform[name](onnx_weights[onnx_name])
                    else:
                        onnx_w = onnx_weights[onnx_name].reshape(w.shape)
                torch_w = w.data.detach().numpy()
                try:
                    check_tensor(onnx_w, torch_w, margin)
                except TestFailureError as e:
                    print("For weight: ", name)
                    raise e


def run_fwd_model(inputs: Union[Iterable[np.ndarray], Mapping[str, np.ndarray]], torch_model: nn.Module) -> Iterable[np.ndarray]:
    def np_to_torch(np_arr: np.ndarray):
        if np_arr.dtype == np.uint32:
            np_arr = np_arr.astype(np.int32)

        torch_input = torch.from_numpy(np_arr)
        if np_arr.dtype == np.int32:
            torch_input = torch_input.long()
        elif np_arr.dtype == np.float:
            torch_input = torch_input.float()
        return torch_input
    if isinstance(inputs, dict):
        torch_inputs = {k: np_to_torch(v) for k, v in inputs.items()}
        torch_outputs = torch_model(**torch_inputs)
    else:
        torch_inputs = map(np_to_torch, inputs)
        torch_outputs = torch_model(*torch_inputs)
    return (t_torch.detach().numpy() for t_torch in make_tuple(torch_outputs))
