# Copyright 2019 Graphcore Ltd.
"""Test EmbeddingSerialised implementation. """
import numpy as np
import pytest
import torch
from tests.torch_bert import BertConfig as TorchBertConfig
from tests.utils import (check_tensors, extract_initializers, run_fwd_model, run_py)
from torch import nn

import popart
import onnx
from bert_model import BertConfig, ExecutionMode, get_model
from phased_execution.bert_layers_serialised import EmbeddingSerialised
from phased_execution.scope_manager import ScopeProvider


test_modes = [ExecutionMode.DEFAULT, pytest.param(ExecutionMode.PHASED, marks=pytest.mark.requires_remote_buffers)]
WEIGHT_NAME = {
    ExecutionMode.PHASED: "Token",
    ExecutionMode.DEFAULT: "Embedding_Dict"
}

ONNX_TO_TORCH = {
    "Embedding_Dict": "weight",
    "Token": "weight"
}

num_splits = 4


def get_initializers(mode, proto, weight_transposed):
    """Get embedding weights from onnx proto.

    Args:
        proto (onnx.proto): Protobuf of onnx model which contains split embedding weights.
        weight_transposed: Construct embedding dict transposed.

    Returns:
        Dict: Mapping of embedding weight name to numpy value.
    """
    initializers = extract_initializers(proto)
    split_tensors = [t for t in initializers if t.startswith(WEIGHT_NAME[mode])]
    torch_name = ONNX_TO_TORCH[WEIGHT_NAME[mode]]
    onnx_wts = [initializers[name].transpose() if weight_transposed else initializers[name]
                for name in split_tensors]
    initializers[torch_name] = np.vstack(onnx_wts)
    return initializers


@pytest.mark.parametrize('mode', test_modes)
@pytest.mark.parametrize('weight_transposed', ('False', 'True'))
@pytest.mark.parametrize('phase', ('fwd', 'bwd'))
def test_split_embedding(mode, weight_transposed, phase, custom_ops):
    """Test serialised embedding.

    Args:
        weight_transposed (bool): If True, weights are constructed transposed for the embedding layer.
        phase (str): Fwd pass or backward pass.
        custom_ops : Custom op module.
    """
    np.random.seed(1984)

    config = BertConfig(vocab_length=4864,
                        batch_size=1,
                        hidden_size=4096,
                        sequence_length=128,
                        popart_dtype="FLOAT",
                        no_dropout=True,
                        embedding_serialization_vocab_steps=num_splits)

    data, outputs, proto, post_proto = popart_result_and_model(config,
                                                               mode,
                                                               weight_transposed,
                                                               is_bwd=(phase == 'bwd'))

    inputs = [t.reshape(config.batch_size, config.sequence_length).astype(np.int32) for t in data]

    torch_output, torch_model = pytorch_result_and_model(config,
                                                         mode,
                                                         inputs,
                                                         proto,
                                                         weight_transposed,
                                                         is_bwd=(phase == 'bwd'))

    check_tensors(torch_output, outputs)
    if phase == 'bwd':
        initializers = get_initializers(mode, post_proto, weight_transposed)
        for name, weight in torch_model.named_parameters():
            check_tensors(weight.data.numpy(), initializers[name])


def popart_result_and_model(config, mode, weight_transposed, is_bwd=False):
    """Run popart model based on config.

    Args:
        config (BertConfig): Popart config.
        weight_transposed: Construct embedding dict transposed.
        is_bwd (bool, optional): Construct training graph if True,
                                 else inference graph. Defaults to False.

    Returns:
        Tuple: Gathered numpy data, outputs from model, proto, post_proto
    """

    scope_provider = ScopeProvider()
    user_options = {}
    if mode == ExecutionMode.PHASED:
        builder = popart.Builder()

        indices_len = config.batch_size * config.sequence_length
        sequence_info = popart.TensorInfo("UINT32", [indices_len])
        indices = builder.addInputTensor(sequence_info)
        data = {indices: np.random.randint(0, config.vocab_length, (indices_len)).astype(np.uint32)}

        popart_model = EmbeddingSerialised(scope_provider.get_scope('Token'),
                                           input_dim=config.vocab_length,
                                           output_dim=config.hidden_size,
                                           num_splits=config.embedding_serialization_vocab_steps,
                                           custom=True,
                                           dtype=config.dtype,
                                           detach=not config.update_embedding_dict,
                                           weight_transposed=weight_transposed,
                                           builder=builder,
                                           scope_provider=scope_provider)
        user_options = {
            "batchSerializationFactor": 1,
            "executionPhases": popart_model.total_execution_phases
        }
        output = popart_model(indices)
    else:
        popart_model = get_model(config, mode, block="embedding", initializers={})
        builder = popart_model.builder

        indices_len = config.batch_size * config.sequence_length
        sequence_info = popart.TensorInfo("UINT32", [indices_len])
        indices = builder.addInputTensor(sequence_info)
        data = {indices: np.random.randint(0, config.vocab_length, (indices_len)).astype(np.uint32)}
        output = popart_model.word_embedding_serialized(indices, num_splits)

    if is_bwd:
        l1_lambda = 0.1
        if mode == ExecutionMode.PHASED:
            loss_scope = scope_provider.get_scope('Loss', 'prev')
            with popart_model.scope_provider(popart_model.builder, loss_scope):
                l1_loss = popart_model.builder.aiGraphcore.l1loss([output],
                                                                  l1_lambda,
                                                                  debugPrefix="l1LossVal",
                                                                  reduction=popart.ReductionType.Sum)
        else:
            l1_loss = popart_model.builder.aiGraphcore.l1loss([output],
                                                              l1_lambda,
                                                              debugPrefix="l1LossVal",
                                                              reduction=popart.ReductionType.Sum)
        proto = builder.getModelProto()
        optimizer = popart.ConstSGD(0.01)
        outputs, post_proto = run_py(proto,
                                     data, (output, l1_loss),
                                     loss=l1_loss,
                                     optimizer=optimizer,
                                     user_options=user_options,
                                     execution_mode=mode)
    else:
        proto = builder.getModelProto()
        outputs, post_proto = run_py(proto, data, output,
                                     user_options=user_options,
                                     execution_mode=mode)

    return [data[indices]], outputs, proto, post_proto


def pytorch_result_and_model(config, mode, inputs, popart_proto, weight_transposed, is_bwd=False):
    """Run pytorch model based on config.

    Args:
        config (BertConfig): Popart config.
        inputs (np.ndarray): Input np array.
        popart_proto (onnx.proto):  Onnx protobuf.
        weight_transposed (bool): If True, onnx weights are constructed transposed.
        is_bwd (bool, optional): True if bwd_pass. Defaults to False.

    Returns:
        Tuple: Output np.array and Torch model.
    """
    torch_config = TorchBertConfig(config.vocab_length,
                                   config.hidden_size,
                                   config.num_layers,
                                   config.attention_heads,
                                   layer_norm_eps=config.layer_norm_eps)
    torch_model = nn.Embedding(torch_config.vocab_size, torch_config.hidden_size, padding_idx=0)
    # Turn off dropout
    torch_model.eval()

    # Conversion of the popart model to onnx
    proto = onnx.load_model_from_string(popart_proto)
    initializers = get_initializers(mode, proto, weight_transposed)


    for name, weight in torch_model.named_parameters():
        weight.data.copy_(torch.from_numpy(initializers[name]).float())

    result = run_fwd_model(inputs, torch_model)

    if is_bwd:
        l1_lambda = 0.1
        optim = torch.optim.SGD(torch_model.parameters(),
                                0.01,
                                weight_decay=0.0,
                                momentum=0.0)

        result = torch_model(*[torch.from_numpy(t).long() for t in inputs])[0]
        torch_loss = l1_lambda * torch.norm(result, 1)
        torch_loss.backward()
        optim.step()
        result = [result.detach().numpy()]

    return result, torch_model
