# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Utilities for testing linear operators."""

import itertools

import tensorflow as tf
from tensorflow.python.framework import test_util
from tensorflow.python.ops.linalg import linear_operator_test_util

from tensorflow_mri.python.linalg import linear_operator_util


DEFAULT_GRAPH_SEED = 876543213


def add_tests(test_cls):
  # Call original add_tests.
  linear_operator_test_util.add_tests(test_cls)

  test_name_dict = {
      "lstsq": _test_lstsq,
      "lstsq_with_broadcast": _test_lstsq_with_broadcast
  }
  optional_tests = []
  tests_with_adjoint_args = [
      "lstsq",
      "lstsq_with_broadcast"
  ]

  for name, test_template_fn in test_name_dict.items():
    if name in test_cls.skip_these_tests():
      continue
    if name in optional_tests and name not in test_cls.optional_tests():
      continue

    for dtype, use_placeholder, shape_info in itertools.product(
        test_cls.dtypes_to_test(),
        test_cls.use_placeholder_options(),
        test_cls.operator_shapes_infos()):
      base_test_name = "_".join([
          "test", name, "_shape={},dtype={},use_placeholder={}".format(
              shape_info.shape, dtype, use_placeholder)])
      if name in tests_with_adjoint_args:
        for adjoint in test_cls.adjoint_options():
          for adjoint_arg in test_cls.adjoint_arg_options():
            test_name = base_test_name + ",adjoint={},adjoint_arg={}".format(
                adjoint, adjoint_arg)
            if hasattr(test_cls, test_name):
              raise RuntimeError("Test %s defined more than once" % test_name)
            setattr(
                test_cls,
                test_name,
                test_util.run_deprecated_v1(
                    test_template_fn(  # pylint: disable=too-many-function-args
                        use_placeholder, shape_info, dtype, adjoint,
                        adjoint_arg, test_cls.use_blockwise_arg())))
      else:
        if hasattr(test_cls, base_test_name):
          raise RuntimeError("Test %s defined more than once" % base_test_name)
        setattr(
            test_cls,
            base_test_name,
            test_util.run_deprecated_v1(test_template_fn(
                use_placeholder, shape_info, dtype)))


OperatorShapesInfo = linear_operator_test_util.OperatorShapesInfo


random_normal = linear_operator_test_util.random_normal
random_uniform = linear_operator_test_util.random_uniform
random_positive_definite_matrix = (
    linear_operator_test_util.random_positive_definite_matrix)


class SquareLinearOperatorDerivedClassTest(
    linear_operator_test_util.SquareLinearOperatorDerivedClassTest):
  pass


class NonSquareLinearOperatorDerivedClassTest(
    linear_operator_test_util.NonSquareLinearOperatorDerivedClassTest):

  def make_rhs(self, operator, adjoint, with_batch=True):
    return self.make_x(operator, adjoint=not adjoint, with_batch=with_batch)


def _test_lstsq(
    use_placeholder, shapes_info, dtype, adjoint, adjoint_arg, blockwise_arg):
  def test_lstsq(self):
    _test_lstsq_base(
        self,
        use_placeholder,
        shapes_info,
        dtype,
        adjoint,
        adjoint_arg,
        blockwise_arg,
        with_batch=True)
  return test_lstsq


def _test_lstsq_with_broadcast(
    use_placeholder, shapes_info, dtype, adjoint, adjoint_arg, blockwise_arg):
  def test_lstsq_with_broadcast(self):
    _test_lstsq_base(
        self,
        use_placeholder,
        shapes_info,
        dtype,
        adjoint,
        adjoint_arg,
        blockwise_arg,
        with_batch=False)
  return test_lstsq_with_broadcast


def _test_lstsq_base(
    self,
    use_placeholder,
    shapes_info,
    dtype,
    adjoint,
    adjoint_arg,
    blockwise_arg,
    with_batch):
  # If batch dimensions are omitted, but there are
  # no batch dimensions for the linear operator, then
  # skip the test case. This is already checked with
  # with_batch=True.
  if not with_batch and len(shapes_info.shape) <= 2:
    return
  with self.session(graph=tf.Graph()) as sess:
    sess.graph.seed = DEFAULT_GRAPH_SEED
    operator, mat = self.operator_and_matrix(
        shapes_info, dtype, use_placeholder=use_placeholder)
    rhs = self.make_rhs(
        operator, adjoint=adjoint, with_batch=with_batch)
    # If adjoint_arg, solve A X = (rhs^H)^H = rhs.
    if adjoint_arg:
      op_solve = operator.lstsq(
          tf.linalg.adjoint(rhs),
          adjoint=adjoint,
          adjoint_arg=adjoint_arg)
    else:
      op_solve = operator.lstsq(
          rhs, adjoint=adjoint, adjoint_arg=adjoint_arg)
    mat_solve = linear_operator_util.matrix_solve_ls_with_broadcast(
        mat, rhs, adjoint=adjoint)
    if not use_placeholder:
      self.assertAllEqual(op_solve.shape,
                          mat_solve.shape)

    # If the operator is blockwise, test both blockwise rhs and `Tensor` rhs;
    # else test only `Tensor` rhs. In both cases, evaluate all results in a
    # single `sess.run` call to avoid re-sampling the random rhs in graph mode.
    if blockwise_arg and len(operator.operators) > 1:
      # pylint: disable=protected-access
      block_dimensions = (
          operator._block_range_dimensions() if adjoint else
          operator._block_domain_dimensions())
      block_dimensions_fn = (
          operator._block_range_dimension_tensors if adjoint else
          operator._block_domain_dimension_tensors)
      # pylint: enable=protected-access
      split_rhs = linear_operator_util.split_arg_into_blocks(
          block_dimensions,
          block_dimensions_fn,
          rhs, axis=-2)
      if adjoint_arg:
        split_rhs = [tf.linalg.adjoint(y) for y in split_rhs]
      split_solve = operator.solve(
          split_rhs, adjoint=adjoint, adjoint_arg=adjoint_arg)
      self.assertEqual(len(split_solve), len(operator.operators))
      split_solve = linear_operator_util.broadcast_matrix_batch_dims(
          split_solve)
      fused_block_solve = tf.concat(split_solve, axis=-2)
      op_solve_v, mat_solve_v, fused_block_solve_v = sess.run([
          op_solve, mat_solve, fused_block_solve])

      # Check that the operator and matrix give the same solution when the rhs
      # is blockwise.
      self.assertAC(mat_solve_v, fused_block_solve_v)
    else:
      op_solve_v, mat_solve_v = sess.run([op_solve, mat_solve])

    # Check that the operator and matrix give the same solution when the rhs is
    # a `Tensor`.
    self.assertAC(op_solve_v, mat_solve_v)
