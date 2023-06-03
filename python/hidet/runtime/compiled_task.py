from typing import List, Dict, Tuple, Union, Any, Optional
import os
import json
import time
from collections import namedtuple
from hidet.runtime.compiled_module import CompiledModule, CompiledFunction, load_compiled_module
from hidet.ir.dtypes import i32
from hidet.ffi import runtime_api
from hidet.ffi.utils import Array


class TaskMetaData:
    def __init__(self, symbols, inputs, outputs, device, num_candidates, hidet_version):
        self.symbols: List[str] = symbols
        self.inputs: List[Union[str, int]] = inputs
        self.outputs: List[Union[str, int]] = outputs
        self.device: str = device
        self.num_candidates: int = num_candidates
        self.hidet_version: str = hidet_version
        self.dynamic_dims: List[Tuple[str, Tuple[int, int]]] = []  # [(name, (tensor_index, dim_index))]
        for tensor_index, sig in enumerate(self.inputs):
            for dim_index, dim in enumerate(sig[1:]):
                if isinstance(dim, str) and dim not in [v for v, _ in self.dynamic_dims]:
                    self.dynamic_dims.append((dim, (tensor_index, dim_index)))

    def export_state(self) -> Dict[str, Any]:
        return {
            'symbols': self.symbols,
            'inputs': self.inputs,
            'outputs': self.outputs,
            'device': self.device,
            'num_candidates': self.num_candidates,
            'hidet_version': self.hidet_version,
        }

    def extract_dynamic_dims(self, inputs) -> Tuple[int, ...]:
        return tuple(inputs[tensor_index].shape[dim_index] for tensor_index, dim_index in self.dynamic_dims)

    @staticmethod
    def from_state(state):
        return TaskMetaData(
            symbols=state['symbols'],
            inputs=state['inputs'],
            outputs=state['outputs'],
            device=state['device'],
            num_candidates=state['num_candidates'],
            hidet_version=state['hidet_version'],
        )


class CompiledTask:
    def __init__(self, task_dir: str):
        self.task_dir: str = task_dir
        self.meta_data: TaskMetaData = self._load_meta_data()
        self.task_module: CompiledModule = load_compiled_module(task_dir)
        self.candidates: List[CompiledFunction] = [
            self.task_module['launch_{}'.format(i)] for i in range(self.meta_data.num_candidates)
        ]
        self.dispatch_table_path = os.path.join(self.task_dir, 'dispatch_table.txt')
        self.dispatch_table: Dict[Tuple[int, ...], int] = self._load_dispatch_table()

        self._get_input_shape = self.task_module['get_input_shape']
        self._get_output_shape = self.task_module['get_output_shape']

    def __call__(self, *args):
        outs = self.run_async(args)
        if len(outs) == 1:
            return outs[0]
        else:
            return outs

    def _load_meta_data(self) -> TaskMetaData:
        meta_data_path = os.path.join(self.task_dir, 'meta.json')
        with open(meta_data_path, 'r') as f:
            meta_data = TaskMetaData.from_state(json.load(f))
        return meta_data

    def _load_compiled_modules(self) -> List[CompiledModule]:
        compiled_modules = []
        candidates_dir = os.path.join(self.task_dir, 'candidates')
        if not os.path.exists(candidates_dir) or not os.path.isdir(candidates_dir):
            raise RuntimeError(f'Cannot find candidates dir: {candidates_dir}')
        for module_dir in os.listdir(candidates_dir):
            if not os.path.isdir(module_dir):
                continue
            compiled_modules.append(CompiledModule(module_dir))
        if len(compiled_modules) == 0:
            raise RuntimeError(f'No compiled module found in {candidates_dir}')
        return compiled_modules

    def _load_dispatch_table(self):
        if not os.path.exists(self.dispatch_table_path):
            return {}
        dispatch_table = {}
        with open(self.dispatch_table_path, 'r') as f:
            for i, line in enumerate(f.readlines()):
                if i == 0:
                    continue
                items = line.split()
                if len(items) == 0:
                    continue
                if len(items) != len(self.meta_data.symbols) + 1:
                    os.remove(self.dispatch_table_path)
                    raise RuntimeError(f'Invalid dispatch table: {self.dispatch_table_path}')
                key = tuple(int(item) for item in items[:-1])
                value = int(items[-1])
                dispatch_table[key] = value
        return dispatch_table

    def _get_symbol_values(self) -> Tuple[int, ...]:
        return tuple(runtime_api.get_symbol_value(symbol) for symbol in self.meta_data.symbols)

    def create_outputs(self):
        import hidet

        dtypes = []
        shapes = []
        for idx, sig in enumerate(self.meta_data.outputs):
            shape_buffer = Array(i32, len(sig) - 1)
            self._get_output_shape(idx, shape_buffer)
            dtypes.append(sig[0])
            shapes.append(list(shape_buffer))
        return [hidet.empty(shape, dtype, device=self.meta_data.device) for shape, dtype in zip(shapes, dtypes)]

    def pick_best_candidate(self, inputs, outputs) -> int:
        import hidet

        key = self._get_symbol_values()
        if key not in self.dispatch_table:
            warmup, number, repeat = hidet.option.get_bench_config()
            latencies = []
            for candidate in self.candidates:
                for _ in range(warmup):
                    candidate(*inputs, *outputs)
                candidate_latency = 0.0
                for _ in range(repeat):
                    hidet.cuda.synchronize()
                    t1 = time.time()
                    for _ in range(number):
                        candidate(*inputs, *outputs)
                    hidet.cuda.synchronize()
                    t2 = time.time()
                    candidate_latency += (t2 - t1) / number
                latencies.append(candidate_latency / repeat)
            self.dispatch_table[key] = latencies.index(min(latencies))

            if not os.path.exists(self.dispatch_table_path):
                with open(self.dispatch_table_path, 'w') as f:
                    f.write(' '.join(self.meta_data.symbols) + '\n')
            with open(self.dispatch_table_path, 'a') as f:
                f.write(' '.join([str(v) for v in key]) + ' ' + str(self.dispatch_table[key]) + '\n')

        candidate_index = self.dispatch_table[key]
        if candidate_index >= len(self.candidates):
            raise RuntimeError(f'Invalid candidate index: {candidate_index}')
        return candidate_index

    def run_async(self, inputs):
        outputs = self.create_outputs()
        candidate = self.candidates[self.pick_best_candidate(inputs, outputs)]
        candidate(*inputs, *outputs)
        return outputs


def load_compiled_task(compiled_task_dir: str) -> CompiledTask:
    return CompiledTask(compiled_task_dir)


CompiledTaskKey = namedtuple('CompiledTaskKey', ['device', 'space', 'task_str'])


class CompiledTaskCache:
    def __init__(self):
        self.cached: Dict[Tuple[str, int, str], CompiledTask] = {}

    def contains(self, device_type: str, space: int, task_str: str) -> bool:
        key = CompiledTaskKey(device_type, space, task_str)
        return key in self.cached

    def get(self, device_type: str, space: int, task_str: str) -> Optional[CompiledTask]:
        key = CompiledTaskKey(device_type, space, task_str)
        return self.cached.get(key) if key in self.cached else None

    def add(self, device_type: str, space: int, task_str: str, compiled_task: CompiledTask):
        key = CompiledTaskKey(device_type, space, task_str)
        self.cached[key] = compiled_task


compiled_task_cache = CompiledTaskCache()