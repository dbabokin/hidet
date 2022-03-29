from hidet.ir.dialects.compute import tensor_input, compute, reduce
from hidet.ir.layout import DataLayout
from hidet.ir.task import Task, Grid
from hidet.ir.type import tensor_type
from hidet.ir.functors import inline_compute


def tuplize(v):
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return v, v


def norm_pad(v):
    if isinstance(v, int):
        return [v, v, v, v]
    elif isinstance(v, (list, tuple)):
        if len(v) == 2:
            return [v[0], v[1], v[0], v[1]]
        elif len(v) == 4:
            return v
    raise NotImplementedError()


def conv2d(batch_size, in_channels, height, width, out_channels, kernel, padding, stride):
    kernel, padding, stride = tuplize(kernel), norm_pad(padding), tuplize(stride)
    input = tensor_input('input', 'float32', [batch_size, in_channels, height, width])
    weight = tensor_input('weight', 'float32', [out_channels, in_channels, kernel[0], kernel[1]])
    padded = compute(
        name='pad',
        shape=[batch_size, in_channels, height + padding[0] + padding[2], weight + padding[1] + padding[3]],
        fcompute=lambda n, c, h, w: input.protect_read(indices=[n, c, h - padding[0], w - padding[1]], default_value=0.0))
    out_height = (height + padding[0] + padding[2] - kernel[0]) // stride[0] + 1
    out_width = (width + padding[1] + padding[3] - kernel[1]) // stride[1] + 1
    output = compute(
        name='out',
        shape=[batch_size, out_channels, out_height, out_width],
        fcompute=lambda n, c, h, w: reduce(
            shape=[in_channels, kernel[0], kernel[1]],
            fcompute=lambda rc, xx, yy: padded[n, rc, h * stride[0] + xx, w * stride[1] + yy] * weight.protect_read(indices=[c, rc, xx, yy], default_value=0.0),
            reduce_type='sum'
        )
    )
    output = inline_compute(output)
    return Task(
        name='conv2d',
        computation=output,
        params=[input, weight, output],
        params_type=[
            tensor_type('global', 'float32', input.shape, layout=DataLayout.row_major(input.shape)),
            tensor_type('global', 'float32', weight.shape, layout=DataLayout.row_major(weight.shape)),
            tensor_type('global', 'float32', output.shape, layout=DataLayout.row_major(output.shape))
        ],
        worker=Grid()
    )
