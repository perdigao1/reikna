import numpy
import pytest

from reikna.helpers import *
from reikna.core import *
from reikna.cluda import functions
from reikna import Transformation, ArrayValue, ScalarValue

import reikna.transformations as transformations

from helpers import *


class Dummy(Computation):
    """
    Dummy computation class with two inputs, two outputs and one parameter.
    Will be used to perform core and transformation tests.
    """

    def _get_argnames(self):
        return ('C', 'D'), ('A', 'B'), ('coeff',)

    def _get_basis_for(self, C, D, A, B, coeff):
        assert C.dtype == D.dtype == A.dtype == B.dtype
        return dict(arr_dtype=C.dtype, coeff_dtype=coeff.dtype, size=C.size)

    def _get_argvalues(self, basis):
        av = ArrayValue((basis.size,), basis.arr_dtype)
        sv = ScalarValue(basis.coeff_dtype)
        return dict(C=av, D=av, A=av, B=av, coeff=sv)

    def _construct_operations(self, basis, device_params):
        operations = self._get_operation_recorder()
        mul = functions.mul(basis.arr_dtype, basis.coeff_dtype)
        div = functions.div(basis.arr_dtype, basis.coeff_dtype)
        template = template_from("""
        <%def name="dummy(C, D, A, B, coeff)">
        ${kernel_definition}
        {
            VIRTUAL_SKIP_THREADS;
            int idx = virtual_global_id(0);
            ${A.ctype} a = ${A.load}(idx);
            ${B.ctype} b = ${B.load}(idx);
            ${C.ctype} c = ${mul}(a, ${coeff});
            ${D.ctype} d = ${div}(b, ${coeff});
            ${C.store}(idx, c);
            ${D.store}(idx, d);
        }
        </%def>

        <%def name="dummy2(CC, DD, C, D)">
        ${kernel_definition}
        {
            VIRTUAL_SKIP_THREADS;
            int idx = virtual_global_id(0);
            ${CC.store}(idx, ${C.load}(idx));
            ${DD.store}(idx, ${D.load}(idx));
        }
        </%def>
        """)

        block_size = 128

        C_temp = operations.add_allocation(basis.size, basis.arr_dtype)
        D_temp = operations.add_allocation(basis.size, basis.arr_dtype)

        operations.add_kernel(
            template.get_def('dummy'),
            [C_temp, D_temp, 'A', 'B', 'coeff'],
            global_size=min_blocks(basis.size, block_size) * block_size,
            local_size=block_size,
            render_kwds=dict(mul=mul, div=div),
            dependencies=[(C_temp, D_temp)])
        operations.add_kernel(
            template.get_def('dummy2'),
            ['C', 'D', C_temp, D_temp],
            global_size=min_blocks(basis.size, block_size) * block_size,
            local_size=block_size,
            dependencies=[('C', 'D')])

        return operations


class DummyNested(Computation):
    """
    Dummy computation class with a nested computation inside.
    """

    def _get_argnames(self):
        return ('C', 'D'), ('A', 'B'), ('coeff',)

    def _get_basis_for(self, C, D, A, B, coeff, fixed_coeff=False):
        assert C.dtype == D.dtype == A.dtype == B.dtype
        return dict(
            arr_dtype=C.dtype, coeff_dtype=coeff.dtype, size=C.size,
            fixed_coeff=coeff if fixed_coeff else None)

    def _get_argvalues(self, basis):
        av = ArrayValue((basis.size,), basis.arr_dtype)
        sv = ScalarValue(basis.coeff_dtype)
        return dict(C=av, D=av, A=av, B=av, coeff=sv)

    def _construct_operations(self, basis, device_params):
        operations = self._get_operation_recorder()
        nested = self.get_nested_computation(Dummy)
        # note that the argument order is changed
        if basis.fixed_coeff is None:
            operations.add_computation(nested, 'D', 'C', 'B', 'A', 'coeff')
        else:
            operations.add_computation(nested, 'D', 'C', 'B', 'A', basis.fixed_coeff)
        return operations


# A function which does the same job as base Dummy kernel
def mock_dummy(a, b, coeff):
    return a * coeff, b / coeff


# Some transformations to use by tests

# Identity transformation: Output = Input
tr_trivial = Transformation(
    inputs=1, outputs=1,
    snippet="${o1.store}(${i1.load});")

# Output = Input1 * Parameter1 + Input 2
tr_2_to_1 = Transformation(
    inputs=2, outputs=1, scalars=1,
    derive_o_from_is=lambda i1, i2, s1: i1,
    derive_render_kwds=lambda o1, i1, i2, s1: dict(
        mul=functions.mul(o1, i1),
        cast=functions.cast(o1, s1)),
    snippet="""
        ${o1.ctype} t = ${mul}(${cast}(${s1}), ${i1.load});
        ${o1.store}(t + ${i2.load});
    """)

# Output1 = Input / 2, Output2 = Input / 2
tr_1_to_2 = Transformation(
    inputs=1, outputs=2,
    derive_render_kwds=lambda o1, o2, i1: dict(
        mul=functions.mul(i1, numpy.float32)),
    snippet="""
        ${o1.ctype} t = ${mul}(${i1.load}, 0.5);
        ${o1.store}(t);
        ${o2.store}(t);
    """)

# Output = Input * Parameter
tr_scale = Transformation(
    inputs=1, outputs=1, scalars=1,
    derive_o_from_is=lambda i1, s1: i1,
    derive_i_from_os=lambda o1, s1: o1,
    derive_render_kwds=lambda o1, i1, s1: dict(
        mul=functions.mul(i1, s1, out_dtype=o1)),
    snippet="${o1.store}(${mul}(${i1.load}, ${s1}));")


def test_non_prepared_call(some_thr):
    d = Dummy(some_thr)
    with pytest.raises(InvalidStateError):
        d(None, None, None, None, None)

def test_incorrect_connections(some_thr):
    d = Dummy(some_thr)
    d.connect(tr_trivial, 'A', ['A_prime'])
    d.connect(tr_trivial, 'D', ['D_prime'])

    tests = [
        # cannot connect to scalar
        (tr_trivial, 'coeff', ['A_prime']),
        # A is not a leaf anymore, should fail
        (tr_trivial, 'A', ['A_prime']),
        # coeff is an existing scalar node, B is an array
        (tr_trivial, 'B', ['coeff']),
        # second list should contain scalar nodes, but A_prime is an array
        (tr_scale, 'C', ['C_prime'], ['A_prime']),
        # incorrect argument name
        (tr_scale, 'C', ['1C_prime'], ['param']),
        # incorrect argument name
        (tr_scale, 'C', ['C_prime'], ['1param']),
        # Cannot connect output to an existing node.
        # With current limitation of strictly elementwise transformations,
        # connection to an existing output node would cause data loss and is most likely an error.
        # Moreover, with current transformation code generator it creates some complications.
        # (Connection to an existing input or scalar is fine, see corresponding tests)
        (tr_trivial, 'C', ['D']),
        (tr_trivial, 'C', ['D_prime']),
        # incorrect number of inputs/outputs
        (tr_1_to_2, 'A', ['A_prime']),
        (tr_2_to_1, 'C', ['C_prime']),
        (tr_trivial, 'A', ['A_prime', 'B_prime']),
        (tr_trivial, 'C', ['C_prime', 'D_prime']),
        (tr_trivial, 'A', ['A_prime'], ['param'])
    ]

    for test in tests:
        with pytest.raises(ValueError):
            d.connect(*test)

def test_non_array_connection(some_thr):
    d = Dummy(some_thr)
    with pytest.raises(ValueError):
        d.connect(tr_trivial, 'coeff', ['A_prime'])

def test_non_existent_connection(some_thr):
    d = Dummy(some_thr)
    with pytest.raises(ValueError):
        d.connect(tr_trivial, 'blah', ['A_prime'])

def test_signature_correctness(some_thr):
    d = Dummy(some_thr)

    # Signature of non-prepared array: no types, no shapes
    assert d.signature_str() == "(array) C, (array) D, (array) A, (array) B, (scalar) coeff"

    # Connect some transformations and prepare
    d.connect(tr_trivial, 'A', ['A_prime'])
    d.connect(tr_2_to_1, 'B', ['A_prime', 'B_prime'], ['B_param'])
    d.connect(tr_trivial, 'B_prime', ['B_new_prime'])
    d.connect(tr_1_to_2, 'C', ['C_half1', 'C_half2'])
    d.connect(tr_trivial, 'C_half1', ['C_new_half1'])
    d.connect(tr_scale, 'D', ['D_prime'], ['D_param'])

    array = ArrayValue((1024,), numpy.complex64)
    scalar = ScalarValue(numpy.float32)

    d.prepare_for(array, array, array, array, array, scalar, scalar, scalar)

    assert d.signature_str() == (
        "(array, complex64, (1024,)) C_new_half1, "
        "(array, complex64, (1024,)) C_half2, "
        "(array, complex64, (1024,)) D_prime, "
        "(array, complex64, (1024,)) A_prime, "
        "(array, complex64, (1024,)) B_new_prime, "
        "(scalar, float32) coeff, "
        "(scalar, float32) D_param, "
        "(scalar, float32) B_param")

def test_incorrect_number_of_arguments_in_prepare(some_thr):
    d = Dummy(some_thr)
    with pytest.raises(TypeError):
        d.prepare_for(None, None, None, None)

def test_incorrect_number_of_arguments_in_call(some_thr):
    array = ArrayValue((1024,), numpy.complex64)
    scalar = ScalarValue(numpy.float32)

    d = Dummy(some_thr)
    d.prepare_for(array, array, array, array, scalar)
    with pytest.raises(TypeError):
        d(None, None, None, None)

def test_scalar_instead_of_array(some_thr):
    N = 1024

    d = Dummy(some_thr)

    A = get_test_array(N, numpy.complex64)
    B = get_test_array(N, numpy.complex64)
    C = get_test_array(N, numpy.complex64)
    D = get_test_array(N, numpy.complex64)

    with pytest.raises(TypeError):
        d.prepare_for(C, D, A, 2, B)
    with pytest.raises(TypeError):
        d.prepare_for(C, D, A, B, B)

def test_debug_signature_check(some_thr):
    N1 = 1024
    N2 = 512

    array = ArrayValue(N1, numpy.complex64)
    scalar = ScalarValue(numpy.float32)

    d = Dummy(some_thr, debug=True)
    d.prepare_for(array, array, array, array, scalar)

    A1 = get_test_array(N1, numpy.complex64)
    B1 = get_test_array(N1, numpy.complex64)
    C1 = get_test_array(N1, numpy.complex64)
    D1 = get_test_array(N1, numpy.complex64)

    A2 = get_test_array(N2, numpy.complex64)
    B2 = get_test_array(N2, numpy.complex64)
    C2 = get_test_array(N2, numpy.complex64)
    D2 = get_test_array(N2, numpy.complex64)

    with pytest.raises(ValueError):
        # this will require basis change
        d(C2, D2, B2, A2, 2)

    with pytest.raises(TypeError):
        # scalar argument in place of array
        d(C1, D1, A1, 2, B1)

    with pytest.raises(TypeError):
        # array argument in place of scalar
        d(C1, D1, A1, B1, B1)

def test_transformations_work(thr):

    coeff = numpy.float32(2)
    B_param = numpy.float32(3)
    D_param = numpy.float32(4)
    N = 1024

    d = Dummy(thr)

    d.connect(tr_trivial, 'A', ['A_prime'])
    d.connect(tr_2_to_1, 'B', ['A_prime', 'B_prime'], ['B_param'])
    d.connect(tr_trivial, 'B_prime', ['B_new_prime'])
    d.connect(tr_1_to_2, 'C', ['C_half1', 'C_half2'])
    d.connect(tr_trivial, 'C_half1', ['C_new_half1'])
    d.connect(tr_scale, 'D', ['D_prime'], ['D_param'])

    A_prime = get_test_array(N, numpy.complex64)
    B_new_prime = get_test_array(N, numpy.complex64)
    gpu_A_prime = thr.to_device(A_prime)
    gpu_B_new_prime = thr.to_device(B_new_prime)
    gpu_C_new_half1 = thr.array(N, numpy.complex64)
    gpu_C_half2 = thr.array(N, numpy.complex64)
    gpu_D_prime = thr.array(N, numpy.complex64)
    d.prepare_for(
        gpu_C_new_half1, gpu_C_half2, gpu_D_prime,
        gpu_A_prime, gpu_B_new_prime,
        coeff, D_param, B_param)

    d(gpu_C_new_half1, gpu_C_half2, gpu_D_prime,
        gpu_A_prime, gpu_B_new_prime, coeff, D_param, B_param)

    A = A_prime
    B = A_prime * B_param + B_new_prime
    C, D = mock_dummy(A, B, coeff)
    C_new_half1 = C / 2
    C_half2 = C / 2
    D_prime = D * D_param

    assert diff_is_negligible(thr.from_device(gpu_C_new_half1), C_new_half1)
    assert diff_is_negligible(thr.from_device(gpu_C_half2), C_half2)
    assert diff_is_negligible(thr.from_device(gpu_D_prime), D_prime)

def test_connection_to_base(thr):

    coeff = numpy.float32(2)
    B_param = numpy.float32(3)
    D_param = numpy.float32(4)
    N = 1024

    d = Dummy(thr)

    # connect to the base array argument (effectively making B the same as A)
    d.connect(tr_trivial, 'A', ['B'])

    # connect to the base scalar argument
    d.connect(tr_scale, 'C', ['C_prime'], ['coeff'])

    B = get_test_array(N, numpy.complex64)
    gpu_B = thr.to_device(B)
    gpu_C_prime = thr.array(N, numpy.complex64)
    gpu_D = thr.array(N, numpy.complex64)
    d.prepare_for(gpu_C_prime, gpu_D, gpu_B, coeff)
    d(gpu_C_prime, gpu_D, gpu_B, coeff)

    A = B
    C, D = mock_dummy(A, B, coeff)
    C_prime = C * coeff

    assert diff_is_negligible(thr.from_device(gpu_C_prime), C_prime)
    assert diff_is_negligible(thr.from_device(gpu_D), D)


@pytest.mark.parametrize('fixed_coeff', [False, True], ids=['var_coeff', 'fixed_coeff'])
def test_nested(thr, fixed_coeff):

    coeff = numpy.float32(2)
    B_param = numpy.float32(3)
    D_param = numpy.float32(4)
    N = 1024

    d = DummyNested(thr)

    d.connect(tr_trivial, 'A', ['A_prime'])
    d.connect(tr_2_to_1, 'B', ['A_prime', 'B_prime'], ['B_param'])
    d.connect(tr_trivial, 'B_prime', ['B_new_prime'])
    d.connect(tr_1_to_2, 'C', ['C_half1', 'C_half2'])
    d.connect(tr_trivial, 'C_half1', ['C_new_half1'])
    d.connect(tr_scale, 'D', ['D_prime'], ['D_param'])

    A_prime = get_test_array(N, numpy.complex64)
    B_new_prime = get_test_array(N, numpy.complex64)
    gpu_A_prime = thr.to_device(A_prime)
    gpu_B_new_prime = thr.to_device(B_new_prime)
    gpu_C_new_half1 = thr.array(N, numpy.complex64)
    gpu_C_half2 = thr.array(N, numpy.complex64)
    gpu_D_prime = thr.array(N, numpy.complex64)
    d.prepare_for(
        gpu_C_new_half1, gpu_C_half2, gpu_D_prime,
        gpu_A_prime, gpu_B_new_prime,
        coeff, D_param, B_param)

    d(gpu_C_new_half1, gpu_C_half2, gpu_D_prime,
        gpu_A_prime, gpu_B_new_prime, coeff, D_param, B_param)

    A = A_prime
    B = A_prime * B_param + B_new_prime
    D, C = mock_dummy(B, A, coeff)
    C_new_half1 = C / 2
    C_half2 = C / 2
    D_prime = D * D_param

    assert diff_is_negligible(thr.from_device(gpu_C_new_half1), C_new_half1)
    assert diff_is_negligible(thr.from_device(gpu_C_half2), C_half2)
    assert diff_is_negligible(thr.from_device(gpu_D_prime), D_prime)


def test_scalar_fixed_type(some_thr):
    """
    Regression test for the bug when explicitly specified type for a scalar argument
    was ignored, and the result of result numpy.min_scalar_type() was used instead.
    """

    N = 1024
    p = numpy.int32(2)
    coeff = numpy.int32(1)

    test = Dummy(some_thr)
    A = some_thr.array(N, numpy.int32)
    B = some_thr.array(N, numpy.int32)
    C = some_thr.array(N, numpy.int32)
    D = some_thr.array(N, numpy.int32)

    test.connect(transformations.scale_param(), 'A', ['A_prime'], ['param'])
    test.prepare_for(C, D, A, B, coeff, p)
    assert test.signature_str() == (
        "(array, int32, (1024,)) C, "
        "(array, int32, (1024,)) D, "
        "(array, int32, (1024,)) A_prime, "
        "(array, int32, (1024,)) B, "
        "(scalar, int32) coeff, "
        "(scalar, int32) param")