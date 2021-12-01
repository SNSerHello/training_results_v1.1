# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
import numpy as np
import json
import pytest
import popart
from tests.utils import run_py, check_tensors, check_onnx_model


def model(splits=1):
    np.random.seed(1984)
    input_data = np.random.randint(0, 20, (4,)).astype(np.uint32)
    weight_data = np.random.rand(4, 20).astype(np.float32)

    builder = popart.Builder()

    d0 = builder.addInputTensor(popart.TensorInfo('UINT32', input_data.shape), 'data0')

    w0 = builder.addInitializedInputTensor(weight_data, 'weight0')
    w0_t = builder.aiOnnx.transpose([w0])

    x = builder.aiOnnx.gather([w0_t, d0])

    x = builder.aiOnnx.matmul([x, w0])
    if splits > 1:
        builder.setSerializeMatMul(
            {x}, 'output_channels', splits, True)
    loss = builder.aiGraphcore.l1loss([x], 0.1, debugPrefix='loss')

    return builder.getModelProto(), {d0: input_data}, x, loss


def session(train=False, skip_execution=False, include_patterns=True, splits=1, outline=False, optim="Sgd"):
    proto, data, x, loss = model(splits=splits)
    # Required
    extraPatterns = []
    if include_patterns:
        extraPatterns += ["TiedGatherPattern", "TiedGatherAccumulatePattern"]
    patterns = popart.Patterns()
    for extraPattern in extraPatterns:
        patterns.enablePattern(extraPattern, True)

    user_options = {
        "enableOutlining": outline,
        "enableGradientAccumulation": True,
        "accumulationFactor": 2,
    }

    if optim == "Lamb":
        optimizer = popart.Adam({
            "defaultLearningRate": (0.1, True),
            "lossScaling": (20, False),
        }, mode=popart.AdamMode.LambNoBias)  # NoBias to increase the error of incorrect gradients
        user_options["optimizerStateTensorLocationSettings"] = popart.TensorLocationSettings(
            popart.TensorLocation(
                popart.TensorStorage.OffChip,
                popart.ReplicatedTensorSharding.On),
            0, 0)
        user_options["enableReplicatedGraphs"] = True
        user_options["replicatedGraphCount"] = 2
    else:
        optimizer = popart.SGD({
            "defaultLearningRate": (0.1, True),
            "defaultMomentum": (0.9, True),
            "defaultDampening": (0, True),
            "lossScaling": (20, False)})  # 0 dampening to increase the error of incorrect gradients

    if train:
        return run_py(
            proto,
            data=data,
            outputs=x,
            loss=loss,
            optimizer=optimizer,
            patterns=patterns,
            user_options=user_options,
            skip_execution=skip_execution)
    else:
        return run_py(
            proto,
            data=data,
            outputs=x,
            patterns=patterns,
            user_options={
                "enableOutlining": outline,
                "constantWeights": False
            },
            skip_execution=skip_execution)


@pytest.mark.sanity
@pytest.mark.parametrize('splits', (1, 4))
@pytest.mark.parametrize(['phase', 'optimizer'], [
    ("fwd", None),
    ("bwd", "Sgd"), ("bwd", "Lamb")])
def test_tied_gather_pattern_ir(splits, phase, optimizer, custom_ops):
    train = phase == "bwd"

    sess = session(train, skip_execution=True, splits=splits, optim=optimizer)

    ir = json.loads(
        sess._serializeIr(popart.IrSerializationFormat.JSON))

    ops = ir["maingraph"]

    # The gatherOp should be replaced with TiedGather
    assert len(list(filter(lambda op: op["type"] == "TiedGather", ops))) == splits
    assert len(list(filter(lambda op: op["type"] == "Gather", ops))) == 0

    # The matmuls should have fully_connected_pass disabled
    assert all(map(lambda op: op["attributes"]["fully_connected_pass"] == '-1',
                   filter(lambda op: op["type"] == "MatMul",
                          ir["maingraph"])))

    if train:
        assert len(list(filter(lambda op: op["type"] == "SparseAccumulate", ops))) == splits


@pytest.mark.sanity
@pytest.mark.parametrize('splits', (1, 4))
@pytest.mark.parametrize(['phase', 'optimizer'], [
    ("fwd", None),
    ("bwd", "Sgd"), ("bwd", "Lamb")])
def test_tied_gather_pattern_correctness(splits, phase, optimizer, custom_ops):
    train = phase == "bwd"

    outputs_1, proto_1 = session(train, skip_execution=False, splits=splits, optim=optimizer)

    outputs_2, proto_2 = session(train, skip_execution=False, include_patterns=False, splits=splits, optim=optimizer)

    check_tensors(outputs_1, outputs_2)
    if train:
        check_onnx_model(proto_1, proto_2)


@pytest.mark.sanity
@pytest.mark.parametrize(['phase'], [(phase,) for phase in ["fwd", "bwd"]])
def test_tied_gather_pattern_outlining_correctness(phase, custom_ops):
    train = phase == "bwd"

    outputs_1, proto_1 = session(train, skip_execution=False, splits=4, outline=True)

    outputs_2, proto_2 = session(train, skip_execution=False, include_patterns=False, splits=4, outline=True)

    check_tensors(outputs_1, outputs_2)
    if train:
        check_onnx_model(proto_1, proto_2)
