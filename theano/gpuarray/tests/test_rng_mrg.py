import functools

import numpy as np

import theano
from theano import change_flags, tensor
from theano.sandbox import rng_mrg
from theano.sandbox.rng_mrg import MRG_RandomStreams
from theano.sandbox.tests.test_rng_mrg import java_samples, rng_mrg_overflow
from theano.sandbox.tests.test_rng_mrg import test_f16_nonzero as cpu_f16_nonzero
from theano.tests import unittest_tools as utt

from .config import mode_with_gpu as mode
from ..type import gpuarray_shared_constructor
from ..rng_mrg import GPUA_mrg_uniform

utt.seed_rng()


def test_consistency_GPUA_serial():
    # Verify that the random numbers generated by GPUA_mrg_uniform, serially,
    # are the same as the reference (Java) implementation by L'Ecuyer et al.

    seed = 12345
    n_samples = 5
    n_streams = 12
    n_substreams = 7

    samples = []
    curr_rstate = np.array([seed] * 6, dtype="int32")

    for i in range(n_streams):
        stream_rstate = curr_rstate.copy()
        for j in range(n_substreams):
            substream_rstate = np.array([stream_rstate.copy()], dtype="int32")
            # Transfer to device
            rstate = gpuarray_shared_constructor(substream_rstate)

            new_rstate, sample = GPUA_mrg_uniform.new(
                rstate, ndim=None, dtype="float32", size=(1,)
            )
            rstate.default_update = new_rstate

            # Not really necessary, just mimicking
            # rng_mrg.MRG_RandomStreams' behavior
            sample.rstate = rstate
            sample.update = (rstate, new_rstate)

            # We need the sample back in the main memory
            cpu_sample = tensor.as_tensor_variable(sample)
            f = theano.function([], cpu_sample, mode=mode)
            for k in range(n_samples):
                s = f()
                samples.append(s)

            # next substream
            stream_rstate = rng_mrg.ff_2p72(stream_rstate)

        # next stream
        curr_rstate = rng_mrg.ff_2p134(curr_rstate)

    samples = np.array(samples).flatten()
    assert np.allclose(samples, java_samples)


def test_consistency_GPUA_parallel():
    # Verify that the random numbers generated by GPUA_mrg_uniform, in
    # parallel, are the same as the reference (Java) implementation by
    # L'Ecuyer et al.
    seed = 12345
    n_samples = 5
    n_streams = 12
    n_substreams = 7  # 7 samples will be drawn in parallel

    samples = []
    curr_rstate = np.array([seed] * 6, dtype="int32")

    for i in range(n_streams):
        stream_samples = []
        rstate = [curr_rstate.copy()]
        for j in range(1, n_substreams):
            rstate.append(rng_mrg.ff_2p72(rstate[-1]))
        rstate = np.asarray(rstate)
        rstate = gpuarray_shared_constructor(rstate)

        new_rstate, sample = GPUA_mrg_uniform.new(
            rstate, ndim=None, dtype="float32", size=(n_substreams,)
        )
        rstate.default_update = new_rstate

        # Not really necessary, just mimicking
        # rng_mrg.MRG_RandomStreams' behavior
        sample.rstate = rstate
        sample.update = (rstate, new_rstate)

        # We need the sample back in the main memory
        cpu_sample = tensor.as_tensor_variable(sample)
        f = theano.function([], cpu_sample, mode=mode)

        for k in range(n_samples):
            s = f()
            stream_samples.append(s)

        samples.append(np.array(stream_samples).T.flatten())

        # next stream
        curr_rstate = rng_mrg.ff_2p134(curr_rstate)

    samples = np.array(samples).flatten()
    assert np.allclose(samples, java_samples)


def test_GPUA_full_fill():
    # Make sure the whole sample buffer is filled.  Also make sure
    # large samples are consistent with CPU results.

    # This needs to be large to trigger the problem on GPU
    size = (10, 1000)

    R = MRG_RandomStreams(234)
    uni = R.uniform(size, nstreams=60 * 256)
    f_cpu = theano.function([], uni)

    rstate_gpu = gpuarray_shared_constructor(R.state_updates[-1][0].get_value())
    new_rstate, sample = GPUA_mrg_uniform.new(
        rstate_gpu, ndim=None, dtype="float32", size=size
    )
    rstate_gpu.default_update = new_rstate
    f_gpu = theano.function([], sample, mode=mode)

    utt.assert_allclose(f_cpu(), f_gpu())


def test_overflow_gpu_new_backend():
    seed = 12345
    n_substreams = 7
    curr_rstate = np.array([seed] * 6, dtype="int32")
    rstate = [curr_rstate.copy()]
    for j in range(1, n_substreams):
        rstate.append(rng_mrg.ff_2p72(rstate[-1]))
    rstate = np.asarray(rstate)
    rstate = gpuarray_shared_constructor(rstate)
    fct = functools.partial(GPUA_mrg_uniform.new, rstate, ndim=None, dtype="float32")
    # should raise error as the size overflows
    sizes = [(2 ** 31,), (2 ** 32,), (2 ** 15, 2 ** 16,), (2, 2 ** 15, 2 ** 15)]
    rng_mrg_overflow(sizes, fct, mode, should_raise_error=True)
    # should not raise error
    sizes = [(2 ** 5,), (2 ** 5, 2 ** 5), (2 ** 5, 2 ** 5, 2 ** 5)]
    rng_mrg_overflow(sizes, fct, mode, should_raise_error=False)
    # should support int32 sizes
    sizes = [(np.int32(2 ** 10),), (np.int32(2), np.int32(2 ** 10), np.int32(2 ** 10))]
    rng_mrg_overflow(sizes, fct, mode, should_raise_error=False)


def test_validate_input_types_gpuarray_backend():
    with change_flags(compute_test_value="raise"):
        rstate = np.zeros((7, 6), dtype="int32")
        rstate = gpuarray_shared_constructor(rstate)
        rng_mrg.mrg_uniform.new(rstate, ndim=None, dtype="float32", size=(3,))


def test_f16_nonzero():
    try:
        # To have theano.shared(x) try to move on the GPU
        theano.compile.shared_constructor(gpuarray_shared_constructor)
        cpu_f16_nonzero(mode=mode, op_to_check=GPUA_mrg_uniform)
    finally:
        theano.compile.shared_constructor(gpuarray_shared_constructor, remove=True)


def test_cpu_target_with_shared_variable():
    srng = MRG_RandomStreams()
    s = np.random.rand(2, 3).astype("float32")
    x = gpuarray_shared_constructor(s, name="x")
    try:
        # To have theano.shared(x) try to move on the GPU
        theano.compile.shared_constructor(gpuarray_shared_constructor)
        y = srng.uniform(x.shape, target="cpu")
        y.name = "y"
        z = (x * y).sum()
        z.name = "z"

        fz = theano.function([], z, mode=mode)

        nodes = fz.maker.fgraph.toposort()
        assert not any([isinstance(node.op, GPUA_mrg_uniform) for node in nodes])
    finally:
        theano.compile.shared_constructor(gpuarray_shared_constructor, remove=True)
