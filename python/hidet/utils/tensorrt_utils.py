from typing import List, Optional, Dict, Tuple
from hashlib import sha256
import os
import time
import numpy as np
import tensorrt as trt
import hidet
from hidet.ffi import cuda_api
from hidet import Tensor, randn, empty
from hidet.utils import hidet_cache_dir


def milo_bytes(MiB):
    return MiB << 20


def create_engine_from_onnx(onnx_model_path: str, workspace_bytes: int = 512 << 20, inputs_shape: Optional[Dict[str, List[int]]] = None) -> trt.ICudaEngine:
    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)

    cache_dir = hidet_cache_dir('trt_engine')
    os.makedirs(cache_dir, exist_ok=True)
    model_name = os.path.basename(onnx_model_path).split('.')[0]
    shape_hash = tuple((name, tuple(shape)) for name, shape in sorted(inputs_shape.items(), key=lambda item: item[0]))
    shape_hash_suffix = sha256(str(shape_hash).encode()).hexdigest()[:6]
    engine_name = '{}_ws{}_{}.engine'.format(model_name, workspace_bytes // (1 << 20), shape_hash_suffix)
    engine_path = os.path.join(cache_dir, engine_name)

    if os.path.exists(engine_path):
        # load the engine directly
        runtime = trt.Runtime(logger)
        with open(engine_path, 'rb') as f:
            serialized_engine = f.read()
        engine = runtime.deserialize_cuda_engine(serialized_engine)
    else:
        # parse onnx model
        network: trt.INetworkDefinition = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        onnx_parser = trt.OnnxParser(network, logger)
        success = onnx_parser.parse_from_file(onnx_model_path)
        for idx in range(onnx_parser.num_errors):
            print(onnx_parser.get_error(idx))
        if not success:
            raise Exception('Failed parse onnx model in tensorrt onnx parser.')

        # set configs of the network builder
        config: trt.IBuilderConfig = builder.create_builder_config()
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, workspace_bytes)

        # optimization profiles required by dynamic inputs
        profile: trt.IOptimizationProfile = builder.create_optimization_profile()
        # assert len(inputs_shape) == network.num_inputs, 'Expect {} number of input shapes'.format(network.num_inputs)
        for i in range(network.num_inputs):
            tensor: trt.ITensor = network.get_input(i)
            if any(v == -1 for v in tensor.shape):
                if inputs_shape is None or tensor.name not in inputs_shape:
                    raise Exception("Found dynamic input: {}{}, "
                                    "please specify input_shapes as the target shape.".format(tensor.name, list(tensor.shape)))
                opt_shape = inputs_shape[tensor.name]
                profile.set_shape(tensor.name, min=opt_shape, opt=opt_shape, max=opt_shape)
        config.add_optimization_profile(profile)

        # build engine
        supported = builder.is_network_supported(network, config)
        if not supported:
            raise Exception('Network is not supported by TensorRT.')
        engine: trt.ICudaEngine = builder.build_engine(network, config)

        # save engine
        serialized_engine = builder.build_serialized_network(network, config)
        with open(engine_path, 'wb') as f:
            f.write(serialized_engine)
    return engine


dtype_map = {
    trt.DataType.INT32: 'int32',
    trt.DataType.FLOAT: 'float32',
}


def _prepare_buffer(engine: trt.ICudaEngine, inputs: Dict[str, Tensor]) -> Tuple[Dict[str, Tensor], Dict[str, Tensor], List[int]]:
    inputs = inputs.copy()
    outputs = {}
    buffers = []
    for i in range(engine.num_bindings):
        name = engine.get_binding_name(i)
        if engine.binding_is_input(i):
            dtype: trt.DataType = engine.get_binding_dtype(i)
            if name not in inputs:
                raise ValueError("TensorRT engine requires input '{}', but only received inputs: {}.".format(name, list(inputs.keys())))
            if dtype != inputs[name].dtype:
                inputs[name] = hidet.tos.operators.cast(inputs[name], dtype_map[dtype])
            buffers.append(inputs[name].storage.addr)
        else:
            shape = engine.get_binding_shape(i)
            dtype: trt.DataType = engine.get_binding_dtype(i)
            output = hidet.empty(shape, dtype_map[dtype], device='cuda')
            outputs[name] = output
            buffers.append(output.storage.addr)
    return inputs, outputs, buffers


def engine_inference(engine: trt.ICudaEngine, inputs: Dict[str, Tensor]) -> Dict[str, Tensor]:
    # prepare inputs and outputs
    inputs, outputs, buffers = _prepare_buffer(engine, inputs)

    # inference
    context: trt.IExecutionContext = engine.create_execution_context()
    context.execute_async_v2(buffers, 0)
    cuda_api.device_synchronization()
    return outputs


def engine_benchmark(engine: trt.ICudaEngine, dummy_inputs: Dict[str, Tensor], warmup: int = 3, number: int = 5, repeat: int = 5) -> List[float]:
    inputs, outputs, buffers = _prepare_buffer(engine, dummy_inputs)
    context: trt.IExecutionContext = engine.create_execution_context()
    for i in range(warmup):
        context.execute_async_v2(buffers, 0)
        cuda_api.device_synchronization()
    results = []
    for i in range(repeat):
        cuda_api.device_synchronization()
        start_time = time.time()
        for j in range(number):
            context.execute_async_v2(buffers, 0)
        cuda_api.device_synchronization()
        end_time = time.time()
        results.append((end_time - start_time) * 1000 / number)
    return results


if __name__ == '__main__':
    # onnx_model_path = os.path.join(hidet_cache_dir('onnx'), 'resnet50-v1-7.onnx')
    onnx_model_path = os.path.join(hidet_cache_dir('onnx'), 'bert-base-uncased.onnx')
    batch_size = 1
    seq_length = 512
    vocab_size = 30522
    input_ids = np.random.randint(0, vocab_size, [batch_size, seq_length], dtype=np.int64)
    attention_mask = np.ones(shape=[batch_size, seq_length], dtype=np.int64)
    token_type_ids = np.zeros(shape=[batch_size, seq_length], dtype=np.int64)

    # onnx
    inputs = {
        'input_ids': hidet.array(input_ids).cuda(),
        'attention_mask': hidet.array(attention_mask).cuda(),
        'token_type_ids': hidet.array(token_type_ids).cuda()
    }
    engine = create_engine_from_onnx(onnx_model_path, inputs_shape={
        key: tensor.shape for key, tensor in inputs.items()
    })
    outputs = engine_inference(engine, inputs)
    results = engine_benchmark(engine, inputs)
    print(results)
