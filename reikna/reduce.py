from reikna.cluda import Snippet
import reikna.helpers as helpers
from reikna.cluda import dtypes
from reikna.core import Computation, Parameter, Annotation, Type
from reikna.transpose import Transpose

TEMPLATE = helpers.template_for(__file__)


class Predicate:
    """
    A predicate used in :py:class:`~reikna.reduce.Reduce`.

    :param operation: a :py:class:`~reikna.cluda.Snippet` object with two parameters
        which will take the names of two arguments to join.
    :param empty: a string with the empty value of the argument
        (the one which, being joined by another argument, does not change it).
    """

    def __init__(self, operation, empty):
        self.operation = operation
        self.empty = empty

    def __process_modules__(self, process):
        return Predicate(process(self.operation), self.empty)


def predicate_sum(dtype):
    """
    Returns a :py:class:`~reikna.reduce.Predicate` object which sums its arguments.
    """
    return Predicate(
        Snippet.create(lambda v1, v2: "return ${v1} + ${v2};"),
        dtypes.c_constant(dtypes.cast(dtype)(0)))


class Reduce(Computation):
    """
    Bases: :py:class:`~reikna.core.Computation`

    Reduces the array over given axis using given binary operation.

    :param arr_t: an array-like defining the initial array.
    :param predicate: a :py:class:`~reikna.reduce.Predicate` object.
    :param axes: a list of non-repeating axes to reduce over.
        If ``None``, the whole array will be reduced
        (in which case the shape of the output array is ``(1,)``).

    .. py:method:: compiled_signature(output:o, input:i)

        :param input: an array with the attributes of ``arr_t``.
        :param output: an array with the attributes of ``arr_t``,
            with its shape missing axes from ``axes``.
    """

    def __init__(self, arr_t, predicate, axes=None):

        dims = len(arr_t.shape)

        if axes is None:
            axes = tuple(range(dims))
        else:
            axes = tuple(sorted(helpers.wrap_in_tuple(axes)))

        if len(set(axes)) != len(axes):
            raise ValueError("Cannot reduce twice over the same axis")

        if min(axes) < 0 or max(axes) >= dims:
            raise ValueError("Axes numbers are out of bounds")

        remaining_axes = tuple(a for a in range(dims) if a not in axes)

        # Currently zero-dimensional arrays are not supported,
        # so we use a 1-element array instead.
        self._real_output_shape = tuple(arr_t.shape[a] for a in remaining_axes)
        if len(self._real_output_shape) == 0:
            output_shape = (1,)
            self._scalar_output = True
        else:
            output_shape = self._real_output_shape
            self._scalar_output = False

        if axes == tuple(range(dims - len(axes), dims)):
            self._transpose_axes = None
        else:
            self._transpose_axes = remaining_axes + axes

        self._predicate = predicate

        Computation.__init__(self, [
            Parameter('output', Annotation(Type(arr_t.dtype, shape=output_shape), 'o')),
            Parameter('input', Annotation(arr_t, 'i'))])

    def _build_plan(self, plan_factory, device_params, output, input_):

        plan = plan_factory()

        # FIXME: may fail if the user passes particularly sophisticated operation
        max_reduce_power = device_params.max_work_group_size

        if self._transpose_axes is None:
            # normal reduction
            cur_input = input_
        else:
            transpose = Transpose(input_, axes=self._transpose_axes)
            tr_output = plan.temp_array_like(transpose.parameter.output)
            plan.computation_call(transpose, tr_output, input_)

            cur_input = tr_output

        axis_start = len(self._real_output_shape)
        axis_end = len(input_.shape) - 1

        input_slices = (axis_start, axis_end - axis_start + 1)

        part_size = helpers.product(cur_input.shape[axis_start:])
        final_size = helpers.product(cur_input.shape[:axis_start])

        while part_size > 1:

            if part_size >= max_reduce_power:
                block_size = max_reduce_power
                blocks_per_part = helpers.min_blocks(part_size, block_size)
                cur_output = plan.temp_array(
                    (final_size, blocks_per_part), input_.dtype)
                output_slices = (1, 1)
            else:
                block_size = helpers.bounding_power_of_2(part_size)
                blocks_per_part = 1
                cur_output = output
                output_slices = (len(cur_output.shape), 0)

            if part_size % block_size != 0:
                last_block_size = part_size % block_size
            else:
                last_block_size = block_size

            render_kwds = dict(
                blocks_per_part=blocks_per_part,
                last_block_size=last_block_size,
                log2=helpers.log2, block_size=block_size,
                warp_size=device_params.warp_size,
                predicate=self._predicate,
                input_slices=input_slices,
                output_slices=output_slices)

            plan.kernel_call(
                TEMPLATE.get_def('reduce'),
                [cur_output, cur_input],
                global_size=(final_size, blocks_per_part * block_size),
                local_size=(1, block_size),
                render_kwds=render_kwds)

            part_size = blocks_per_part
            cur_input = cur_output
            input_slices = output_slices

        return plan
