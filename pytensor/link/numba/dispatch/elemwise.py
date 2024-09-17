from collections.abc import Callable
from functools import singledispatch
from numbers import Number
from textwrap import indent
from typing import Any

import numba
import numpy as np
from numba.core.extending import overload
from numpy.core.numeric import normalize_axis_index, normalize_axis_tuple

from pytensor import config
from pytensor.graph.basic import Apply
from pytensor.graph.op import Op
from pytensor.link.numba.dispatch import basic as numba_basic
from pytensor.link.numba.dispatch.basic import (
    create_numba_signature,
    create_tuple_creator,
    numba_funcify,
    numba_njit,
    use_optimized_cheap_pass,
)
from pytensor.link.numba.dispatch.vectorize_codegen import (
    _jit_options,
    _vectorized,
    encode_literals,
    store_core_outputs,
)
from pytensor.link.utils import compile_function_src, get_name_for_object
from pytensor.scalar.basic import (
    AND,
    OR,
    XOR,
    Add,
    Composite,
    IntDiv,
    Mean,
    Mul,
    ScalarMaximum,
    ScalarMinimum,
    Sub,
    TrueDiv,
    scalar_maximum,
)
from pytensor.scalar.basic import add as add_as
from pytensor.tensor.elemwise import CAReduce, DimShuffle, Elemwise
from pytensor.tensor.math import Argmax, MulWithoutZeros, Sum
from pytensor.tensor.special import LogSoftmax, Softmax, SoftmaxGrad
from pytensor.tensor.type import scalar


@singledispatch
def scalar_in_place_fn(op: Op, idx: str, res: str, arr: str):
    """Return code for an in-place update on an array using a binary scalar :class:`Op`.

    Parameters
    ----------
    op
        The scalar :class:`Op`
    idx
        The index of `res` that needs to be updated.
    res
        The symbol name for the first input and results/output.
    arr
        The symbol name for the second input.
    """
    raise NotImplementedError()


@scalar_in_place_fn.register(Add)
def scalar_in_place_fn_Add(op, idx, res, arr):
    return f"{res}[{idx}] += {arr}"


@scalar_in_place_fn.register(Sub)
def scalar_in_place_fn_Sub(op, idx, res, arr):
    return f"{res}[{idx}] -= {arr}"


@scalar_in_place_fn.register(Mean)
def scalar_in_place_fn_Mean(op, idx, res, arr):
    return f"{res}[{idx}] += ({arr} - {res}[{idx}]) / (i + 1)"


@scalar_in_place_fn.register(Mul)
def scalar_in_place_fn_Mul(op, idx, res, arr):
    return f"{res}[{idx}] *= {arr}"


@scalar_in_place_fn.register(MulWithoutZeros)
def scalar_in_place_fn_MulWithoutZeros(op, idx, res, arr):
    return f"{res}[{idx}] = {arr} if {res}[{idx}] == 0 else ({res}[{idx}] if {arr} == 0 else {res}[{idx}] * {arr})"


@scalar_in_place_fn.register(AND)
def scalar_in_place_fn_AND(op, idx, res, arr):
    return f"{res}[{idx}] &= {arr}"


@scalar_in_place_fn.register(OR)
def scalar_in_place_fn_OR(op, idx, res, arr):
    return f"{res}[{idx}] |= {arr}"


@scalar_in_place_fn.register(XOR)
def scalar_in_place_fn_XOR(op, idx, res, arr):
    return f"{res}[{idx}] ^= {arr}"


@scalar_in_place_fn.register(TrueDiv)
def scalar_in_place_fn_TrueDiv(op, idx, res, arr):
    return f"{res}[{idx}] /= {arr}"


@scalar_in_place_fn.register(IntDiv)
def scalar_in_place_fn_IntDiv(op, idx, res, arr):
    return f"{res}[{idx}] //= {arr}"


@scalar_in_place_fn.register(ScalarMaximum)
def scalar_in_place_fn_ScalarMaximum(op, idx, res, arr):
    return f"""
if {res}[{idx}] < {arr}:
    {res}[{idx}] = {arr}
"""


@scalar_in_place_fn.register(ScalarMinimum)
def scalar_in_place_fn_ScalarMinimum(op, idx, res, arr):
    return f"""
if {res}[{idx}] > {arr}:
    {res}[{idx}] = {arr}
"""


def create_vectorize_func(
    scalar_op_fn: Callable,
    node: Apply,
    use_signature: bool = False,
    identity: Any | None = None,
    **kwargs,
) -> Callable:
    r"""Create a vectorized Numba function from a `Apply`\s Python function."""

    if len(node.outputs) > 1:
        raise NotImplementedError(
            "Multi-output Elemwise Ops are not supported by the Numba backend"
        )

    if use_signature:
        signature = [create_numba_signature(node, force_scalar=True)]
    else:
        signature = []

    target = (
        getattr(node.tag, "numba__vectorize_target", None)
        or config.numba__vectorize_target
    )

    numba_vectorized_fn = numba_basic.numba_vectorize(
        signature, identity=identity, target=target, fastmath=config.numba__fastmath
    )

    py_scalar_func = getattr(scalar_op_fn, "py_func", scalar_op_fn)

    elemwise_fn = numba_vectorized_fn(scalar_op_fn)
    elemwise_fn.py_scalar_func = py_scalar_func

    return elemwise_fn


def create_axis_reducer(
    scalar_op: Op,
    identity: np.ndarray | Number,
    axis: int,
    ndim: int,
    dtype: numba.types.Type,
    keepdims: bool = False,
    return_scalar=False,
) -> numba.core.dispatcher.Dispatcher:
    r"""Create Python function that performs a NumPy-like reduction on a given axis.

    The functions generated by this function take the following form:

    .. code-block:: python

        def careduce_axis(x):
            res_shape = tuple(
                shape[i] if i < axis else shape[i + 1] for i in range(ndim - 1)
            )
            res = np.full(res_shape, identity, dtype=dtype)

            x_axis_first = x.transpose(reaxis_first)

            for m in range(x.shape[axis]):
                reduce_fn(res, x_axis_first[m], res)

            if keepdims:
                return np.expand_dims(res, axis)
            else:
                return res


    This can be removed/replaced when
    https://github.com/numba/numba/issues/4504 is implemented.

    Parameters
    ==========
    scalar_op:
        The scalar :class:`Op` that performs the desired reduction.
    identity:
        The identity value for the reduction.
    axis:
        The axis to reduce.
    ndim:
        The number of dimensions of the result.
    dtype:
        The data type of the result.
    keepdims:
        Determines whether or not the reduced dimension is retained.


    Returns
    =======
    A Python function that can be JITed.

    """

    axis = normalize_axis_index(axis, ndim)

    reduce_elemwise_fn_name = "careduce_axis"

    identity = str(identity)
    if identity == "inf":
        identity = "np.inf"
    elif identity == "-inf":
        identity = "-np.inf"

    global_env = {
        "np": np,
        "numba_basic": numba_basic,
        "out_dtype": dtype,
    }

    if ndim > 1:
        res_shape_tuple_ctor = create_tuple_creator(
            lambda i, shape: shape[i] if i < axis else shape[i + 1], ndim - 1
        )
        global_env["res_shape_tuple_ctor"] = res_shape_tuple_ctor

        res_indices = []
        arr_indices = []
        count = 0

        for i in range(ndim):
            if i == axis:
                arr_indices.append("i")
            else:
                res_indices.append(f"idx_arr[{count}]")
                arr_indices.append(f"idx_arr[{count}]")
                count = count + 1

        res_indices = ", ".join(res_indices)
        arr_indices = ", ".join(arr_indices)

        inplace_update_statement = scalar_in_place_fn(
            scalar_op, res_indices, "res", f"x[{arr_indices}]"
        )
        inplace_update_statement = indent(inplace_update_statement, " " * 4 * 3)

        return_expr = f"np.expand_dims(res, {axis})" if keepdims else "res"
        reduce_elemwise_def_src = f"""
def {reduce_elemwise_fn_name}(x):

    x_shape = np.shape(x)
    res_shape = res_shape_tuple_ctor(x_shape)
    res = np.full(res_shape, numba_basic.to_scalar({identity}), dtype=out_dtype)

    axis_shape = x.shape[{axis}]

    for idx_arr in np.ndindex(res_shape):
        for i in range(axis_shape):
{inplace_update_statement}

    return {return_expr}
        """
    else:
        inplace_update_statement = scalar_in_place_fn(scalar_op, "0", "res", "x[i]")
        inplace_update_statement = indent(inplace_update_statement, " " * 4 * 2)

        return_expr = "res" if keepdims else "res.item()"
        if not return_scalar:
            return_expr = f"np.asarray({return_expr})"
        reduce_elemwise_def_src = f"""
def {reduce_elemwise_fn_name}(x):

    res = np.full(1, numba_basic.to_scalar({identity}), dtype=out_dtype)

    axis_shape = x.shape[{axis}]

    for i in range(axis_shape):
{inplace_update_statement}

    return {return_expr}
        """

    reduce_elemwise_fn_py = compile_function_src(
        reduce_elemwise_def_src, reduce_elemwise_fn_name, {**globals(), **global_env}
    )

    return reduce_elemwise_fn_py


def create_multiaxis_reducer(
    scalar_op,
    identity,
    axes,
    ndim,
    dtype,
    input_name="input",
    return_scalar=False,
):
    r"""Construct a function that reduces multiple axes.

    The functions generated by this function take the following form:

    .. code-block:: python

        def careduce_maximum(input):
            axis_0_res = careduce_axes_fn_0(input)
            axis_1_res = careduce_axes_fn_1(axis_0_res)
            ...
            axis_N_res = careduce_axes_fn_N(axis_N_minus_1_res)
            return axis_N_res

    The range 0-N is determined by the `axes` argument (i.e. the
    axes to be reduced).


    Parameters
    ==========
    scalar_op:
        The scalar :class:`Op` that performs the desired reduction.
    identity:
        The identity value for the reduction.
    axes:
        The axes to reduce.
    ndim:
        The number of dimensions of the result.
    dtype:
        The data type of the result.
    return_scalar:
        If True, return a scalar, otherwise an array.

    Returns
    =======
    A Python function that can be JITed.

    """
    if len(axes) == 1:
        return create_axis_reducer(scalar_op, identity, axes[0], ndim, dtype)

    axes = normalize_axis_tuple(axes, ndim)

    careduce_fn_name = f"careduce_{scalar_op}"
    global_env = {}
    to_reduce = sorted(axes, reverse=True)
    careduce_lines_src = []
    var_name = input_name

    for i, axis in enumerate(to_reduce):
        careducer_axes_fn_name = f"careduce_axes_fn_{i}"
        reducer_py_fn = create_axis_reducer(scalar_op, identity, axis, ndim, dtype)
        reducer_fn = numba_basic.numba_njit(
            boundscheck=False, fastmath=config.numba__fastmath
        )(reducer_py_fn)

        global_env[careducer_axes_fn_name] = reducer_fn

        ndim -= 1
        last_var_name = var_name
        var_name = f"axis_{i}_res"
        careduce_lines_src.append(
            f"{var_name} = {careducer_axes_fn_name}({last_var_name})"
        )

    careduce_assign_lines = indent("\n".join(careduce_lines_src), " " * 4)
    if not return_scalar:
        pre_result = "np.asarray"
        post_result = ""
    else:
        pre_result = "np.asarray"
        post_result = ".item()"

    careduce_def_src = f"""
def {careduce_fn_name}({input_name}):
{careduce_assign_lines}
    return {pre_result}({var_name}){post_result}
    """

    careduce_fn = compile_function_src(
        careduce_def_src, careduce_fn_name, {**globals(), **global_env}
    )

    return careduce_fn


def jit_compile_reducer(
    node, fn, *, reduce_to_scalar=False, infer_signature=True, **kwds
):
    """Compile Python source for reduction loops using additional optimizations.

    Parameters
    ==========
    node
        An node from which the signature can be derived.
    fn
        The Python function object to compile.
    reduce_to_scalar: bool, default False
        Whether to reduce output to a scalar (instead of 0d array)
    infer_signature: bool: default True
        Whether to try and infer the function signature from the Apply node.
    kwds
        Extra keywords to be added to the :func:`numba.njit` function.

    Returns
    =======
    A :func:`numba.njit`-compiled function.

    """
    if infer_signature:
        signature = create_numba_signature(node, reduce_to_scalar=reduce_to_scalar)
        args = (signature,)
    else:
        args = ()

    # Eagerly compile the function using increased optimizations.  This should
    # help improve nested loop reductions.
    with use_optimized_cheap_pass():
        res = numba_basic.numba_njit(
            *args,
            boundscheck=False,
            fastmath=config.numba__fastmath,
            **kwds,
        )(fn)

    return res


def create_axis_apply_fn(fn, axis, ndim, dtype):
    axis = normalize_axis_index(axis, ndim)

    reaxis_first = (*(i for i in range(ndim) if i != axis), axis)

    @numba_basic.numba_njit(boundscheck=False)
    def axis_apply_fn(x):
        x_reaxis = x.transpose(reaxis_first)

        res = np.zeros(x_reaxis.shape[:-1], dtype=dtype)
        for m in np.ndindex(res.shape):
            v = fn(x_reaxis[m])
            res[m] = v
        return res

    return axis_apply_fn


@numba_funcify.register(Elemwise)
def numba_funcify_Elemwise(op, node, **kwargs):
    # Creating a new scalar node is more involved and unnecessary
    # if the scalar_op is composite, as the fgraph already contains
    # all the necessary information.
    scalar_node = None
    if not isinstance(op.scalar_op, Composite):
        scalar_inputs = [scalar(dtype=input.dtype) for input in node.inputs]
        scalar_node = op.scalar_op.make_node(*scalar_inputs)

    scalar_op_fn = numba_funcify(
        op.scalar_op,
        node=scalar_node,
        parent_node=node,
        fastmath=_jit_options["fastmath"],
        **kwargs,
    )

    nin = len(node.inputs)
    nout = len(node.outputs)
    core_op_fn = store_core_outputs(scalar_op_fn, nin=nin, nout=nout)

    input_bc_patterns = tuple(inp.type.broadcastable for inp in node.inputs)
    output_bc_patterns = tuple(out.type.broadcastable for out in node.outputs)
    output_dtypes = tuple(out.type.dtype for out in node.outputs)
    inplace_pattern = tuple(op.inplace_pattern.items())
    core_output_shapes = tuple(() for _ in range(nout))

    # numba doesn't support nested literals right now...
    input_bc_patterns_enc = encode_literals(input_bc_patterns)
    output_bc_patterns_enc = encode_literals(output_bc_patterns)
    output_dtypes_enc = encode_literals(output_dtypes)
    inplace_pattern_enc = encode_literals(inplace_pattern)

    def elemwise_wrapper(*inputs):
        return _vectorized(
            core_op_fn,
            input_bc_patterns_enc,
            output_bc_patterns_enc,
            output_dtypes_enc,
            inplace_pattern_enc,
            (),  # constant_inputs
            inputs,
            core_output_shapes,  # core_shapes
            None,  # size
        )

    # Pure python implementation, that will be used in tests
    def elemwise(*inputs):
        inputs = [np.asarray(input) for input in inputs]
        inputs_bc = np.broadcast_arrays(*inputs)
        shape = inputs[0].shape
        for input, bc in zip(inputs, input_bc_patterns):
            for length, allow_bc, iter_length in zip(input.shape, bc, shape):
                if length == 1 and shape and iter_length != 1 and not allow_bc:
                    raise ValueError("Broadcast not allowed.")

        outputs = [np.empty(shape, dtype=dtype) for dtype in output_dtypes]

        for idx in np.ndindex(shape):
            vals = [input[idx] for input in inputs_bc]
            outs = scalar_op_fn(*vals)
            if not isinstance(outs, tuple):
                outs = (outs,)
            for out, out_val in zip(outputs, outs):
                out[idx] = out_val

        outputs_summed = []
        for output, bc in zip(outputs, output_bc_patterns):
            axes = tuple(np.nonzero(bc)[0])
            outputs_summed.append(output.sum(axes, keepdims=True))
        if len(outputs_summed) != 1:
            return tuple(outputs_summed)
        return outputs_summed[0]

    @overload(elemwise, jit_options=_jit_options)
    def ov_elemwise(*inputs):
        return elemwise_wrapper

    return elemwise


@numba_funcify.register(Sum)
def numba_funcify_Sum(op, node, **kwargs):
    axes = op.axis
    if axes is None:
        axes = list(range(node.inputs[0].ndim))

    axes = tuple(axes)

    ndim_input = node.inputs[0].ndim

    if hasattr(op, "acc_dtype") and op.acc_dtype is not None:
        acc_dtype = op.acc_dtype
    else:
        acc_dtype = node.outputs[0].type.dtype

    np_acc_dtype = np.dtype(acc_dtype)

    out_dtype = np.dtype(node.outputs[0].dtype)

    if ndim_input == len(axes):

        @numba_njit(fastmath=True)
        def impl_sum(array):
            return np.asarray(array.sum(), dtype=np_acc_dtype).astype(out_dtype)

    elif len(axes) == 0:

        @numba_njit(fastmath=True)
        def impl_sum(array):
            return np.asarray(array, dtype=out_dtype)

    else:
        impl_sum = numba_funcify_CAReduce(op, node, **kwargs)

    return impl_sum


@numba_funcify.register(CAReduce)
def numba_funcify_CAReduce(op, node, **kwargs):
    axes = op.axis
    if axes is None:
        axes = list(range(node.inputs[0].ndim))

    if hasattr(op, "acc_dtype") and op.acc_dtype is not None:
        acc_dtype = op.acc_dtype
    else:
        acc_dtype = node.outputs[0].type.dtype

    np_acc_dtype = np.dtype(acc_dtype)

    scalar_op_identity = op.scalar_op.identity
    if np_acc_dtype.kind == "i" and not np.isfinite(scalar_op_identity):
        if np.isposinf(scalar_op_identity):
            scalar_op_identity = np.iinfo(np_acc_dtype).max
        else:
            scalar_op_identity = np.iinfo(np_acc_dtype).min

    # Make sure it has the correct dtype
    scalar_op_identity = np.array(scalar_op_identity, dtype=np_acc_dtype)

    input_name = get_name_for_object(node.inputs[0])
    ndim = node.inputs[0].ndim
    careduce_py_fn = create_multiaxis_reducer(
        op.scalar_op,
        scalar_op_identity,
        axes,
        ndim,
        np.dtype(node.outputs[0].type.dtype),
        input_name=input_name,
    )

    careduce_fn = jit_compile_reducer(node, careduce_py_fn, reduce_to_scalar=False)
    return careduce_fn


@numba_funcify.register(DimShuffle)
def numba_funcify_DimShuffle(op, node, **kwargs):
    shuffle = tuple(op.shuffle)
    transposition = tuple(op.transposition)
    augment = tuple(op.augment)
    inplace = op.inplace

    ndim_new_shape = len(shuffle) + len(augment)

    no_transpose = all(i == j for i, j in enumerate(transposition))
    if no_transpose:

        @numba_basic.numba_njit
        def transpose(x):
            return x

    else:

        @numba_basic.numba_njit
        def transpose(x):
            return np.transpose(x, transposition)

    shape_template = (1,) * ndim_new_shape

    # When `len(shuffle) == 0`, the `shuffle_shape[j]` expression below
    # is typed as `getitem(Tuple(), int)`, which has no implementation
    # (since getting an item from an empty sequence doesn't make sense).
    # To avoid this compile-time error, we omit the expression altogether.
    if len(shuffle) > 0:
        # Use the statically known shape if available
        if all(length is not None for length in node.outputs[0].type.shape):
            shape = node.outputs[0].type.shape

            @numba_basic.numba_njit
            def find_shape(array_shape):
                return shape

        else:

            @numba_basic.numba_njit
            def find_shape(array_shape):
                shape = shape_template
                j = 0
                for i in range(ndim_new_shape):
                    if i not in augment:
                        length = array_shape[j]
                        shape = numba_basic.tuple_setitem(shape, i, length)
                        j = j + 1
                return shape

    else:

        @numba_basic.numba_njit
        def find_shape(array_shape):
            return shape_template

    if ndim_new_shape > 0:

        @numba_basic.numba_njit
        def dimshuffle_inner(x, shuffle):
            x = transpose(x)
            shuffle_shape = x.shape[: len(shuffle)]
            new_shape = find_shape(shuffle_shape)

            # FIXME: Numba's `array.reshape` only accepts C arrays.
            res_reshape = np.reshape(np.ascontiguousarray(x), new_shape)

            if not inplace:
                return res_reshape.copy()
            else:
                return res_reshape

    else:

        @numba_basic.numba_njit
        def dimshuffle_inner(x, shuffle):
            return np.reshape(np.ascontiguousarray(x), ())

    # Without the following wrapper function we would see this error:
    # E   No implementation of function Function(<built-in function getitem>) found for signature:
    # E
    # E    >>> getitem(UniTuple(int64 x 2), slice<a:b>)
    # E
    # E   There are 22 candidate implementations:
    # E      - Of which 22 did not match due to:
    # E      Overload of function 'getitem': File: <numerous>: Line N/A.
    # E        With argument(s): '(UniTuple(int64 x 2), slice<a:b>)':
    # E       No match.
    # ...(on this line)...
    # E           shuffle_shape = res.shape[: len(shuffle)]
    @numba_basic.numba_njit(inline="always")
    def dimshuffle(x):
        return dimshuffle_inner(np.asarray(x), shuffle)

    return dimshuffle


@numba_funcify.register(Softmax)
def numba_funcify_Softmax(op, node, **kwargs):
    x_at = node.inputs[0]
    x_dtype = x_at.type.numpy_dtype
    x_dtype = numba.np.numpy_support.from_dtype(x_dtype)
    axis = op.axis

    if axis is not None:
        axis = normalize_axis_index(axis, x_at.ndim)
        reduce_max_py = create_axis_reducer(
            scalar_maximum, -np.inf, axis, x_at.ndim, x_dtype, keepdims=True
        )
        reduce_sum_py = create_axis_reducer(
            add_as, 0.0, axis, x_at.ndim, x_dtype, keepdims=True
        )

        jit_fn = numba_basic.numba_njit(
            boundscheck=False, fastmath=config.numba__fastmath
        )
        reduce_max = jit_fn(reduce_max_py)
        reduce_sum = jit_fn(reduce_sum_py)
    else:
        reduce_max = np.max
        reduce_sum = np.sum

    def softmax_py_fn(x):
        z = reduce_max(x)
        e_x = np.exp(x - z)
        w = reduce_sum(e_x)
        sm = e_x / w
        return sm

    softmax = jit_compile_reducer(node, softmax_py_fn)

    return softmax


@numba_funcify.register(SoftmaxGrad)
def numba_funcify_SoftmaxGrad(op, node, **kwargs):
    sm_at = node.inputs[1]
    sm_dtype = sm_at.type.numpy_dtype
    sm_dtype = numba.np.numpy_support.from_dtype(sm_dtype)

    axis = op.axis
    if axis is not None:
        axis = normalize_axis_index(axis, sm_at.ndim)
        reduce_sum_py = create_axis_reducer(
            add_as, 0.0, axis, sm_at.ndim, sm_dtype, keepdims=True
        )

        jit_fn = numba_basic.numba_njit(
            boundscheck=False, fastmath=config.numba__fastmath
        )
        reduce_sum = jit_fn(reduce_sum_py)
    else:
        reduce_sum = np.sum

    def softmax_grad_py_fn(dy, sm):
        dy_times_sm = dy * sm
        sum_dy_times_sm = reduce_sum(dy_times_sm)
        dx = dy_times_sm - sum_dy_times_sm * sm
        return dx

    # The signature inferred by jit_compile_reducer is wrong when dy is a constant (readonly=True)
    softmax_grad = jit_compile_reducer(node, softmax_grad_py_fn, infer_signature=False)

    return softmax_grad


@numba_funcify.register(LogSoftmax)
def numba_funcify_LogSoftmax(op, node, **kwargs):
    x_at = node.inputs[0]
    x_dtype = x_at.type.numpy_dtype
    x_dtype = numba.np.numpy_support.from_dtype(x_dtype)
    axis = op.axis

    if axis is not None:
        axis = normalize_axis_index(axis, x_at.ndim)
        reduce_max_py = create_axis_reducer(
            scalar_maximum,
            -np.inf,
            axis,
            x_at.ndim,
            x_dtype,
            keepdims=True,
        )
        reduce_sum_py = create_axis_reducer(
            add_as, 0.0, axis, x_at.ndim, x_dtype, keepdims=True
        )

        jit_fn = numba_basic.numba_njit(
            boundscheck=False, fastmath=config.numba__fastmath
        )
        reduce_max = jit_fn(reduce_max_py)
        reduce_sum = jit_fn(reduce_sum_py)
    else:
        reduce_max = np.max
        reduce_sum = np.sum

    def log_softmax_py_fn(x):
        xdev = x - reduce_max(x)
        lsm = xdev - np.log(reduce_sum(np.exp(xdev)))
        return lsm

    log_softmax = jit_compile_reducer(node, log_softmax_py_fn)
    return log_softmax


@numba_funcify.register(Argmax)
def numba_funcify_Argmax(op, node, **kwargs):
    axis = op.axis
    x_at = node.inputs[0]
    x_dtype = x_at.type.numpy_dtype
    x_dtype = numba.np.numpy_support.from_dtype(x_dtype)
    x_ndim = x_at.ndim

    if x_ndim == 0:

        @numba_basic.numba_njit(inline="always")
        def argmax(x):
            return 0

    else:
        axes = tuple(int(ax) for ax in axis)

        # NumPy does not support multiple axes for argmax; this is a
        # work-around
        keep_axes = tuple(i for i in range(x_ndim) if i not in axes)

        reduced_x_ndim = x_ndim - len(axes) + 1
        argmax_axis = create_axis_apply_fn(
            np.argmax, reduced_x_ndim - 1, reduced_x_ndim, np.int64
        )

        reaxis_order = keep_axes + axes
        sl1 = slice(None, len(keep_axes))
        sl2 = slice(len(keep_axes), None)

        @numba_basic.numba_njit
        def argmax(x):
            # Not-reduced axes in front
            transposed_x = np.ascontiguousarray(np.transpose(x, reaxis_order))
            kept_shape = transposed_x.shape[sl1]
            reduced_shape = transposed_x.shape[sl2]
            reduced_size = 1
            for s in reduced_shape:
                reduced_size *= s

            # Numpy.prod returns 1.0 when arg is empty, so we cast it to int64
            # Otherwise reshape would complain citing float arg
            new_shape = (*kept_shape, reduced_size)
            reshaped_x = transposed_x.reshape(new_shape)

            max_idx_res = argmax_axis(reshaped_x)

            return max_idx_res

    return argmax
