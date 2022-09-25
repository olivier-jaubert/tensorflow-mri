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
"""Tests for base linear operator."""

import numpy as np
import tensorflow as tf

from tensorflow_mri.python.linalg import linear_operator
from tensorflow_mri.python.linalg import linear_operator_full_matrix
from tensorflow_mri.python.util import test_util


rng = np.random.RandomState(123)


class LinearOperatorShape(linear_operator.LinearOperator):
  """LinearOperator that implements the methods ._shape and _shape_tensor."""

  def __init__(self,
               shape,
               is_non_singular=None,
               is_self_adjoint=None,
               is_positive_definite=None,
               is_square=None):
    parameters = dict(
        shape=shape,
        is_non_singular=is_non_singular,
        is_self_adjoint=is_self_adjoint,
        is_positive_definite=is_positive_definite,
        is_square=is_square
    )

    self._stored_shape = shape
    super(LinearOperatorShape, self).__init__(
        dtype=tf.float32,
        is_non_singular=is_non_singular,
        is_self_adjoint=is_self_adjoint,
        is_positive_definite=is_positive_definite,
        is_square=is_square,
        parameters=parameters)

  def _shape(self):
    return tf.TensorShape(self._stored_shape)

  def _shape_tensor(self):
    return tf.constant(self._stored_shape, dtype=tf.int32)

  def _matmul(self):
    raise NotImplementedError("Not needed for this test.")


class LinearOperatorMatmulSolve(linear_operator.LinearOperator):
  """LinearOperator that wraps a [batch] matrix and implements matmul/solve."""

  def __init__(self,
               matrix,
               is_non_singular=None,
               is_self_adjoint=None,
               is_positive_definite=None,
               is_square=None):
    parameters = dict(
        matrix=matrix,
        is_non_singular=is_non_singular,
        is_self_adjoint=is_self_adjoint,
        is_positive_definite=is_positive_definite,
        is_square=is_square
    )

    self._matrix = tf.convert_to_tensor(matrix, name="matrix")
    super(LinearOperatorMatmulSolve, self).__init__(
        dtype=self._matrix.dtype,
        is_non_singular=is_non_singular,
        is_self_adjoint=is_self_adjoint,
        is_positive_definite=is_positive_definite,
        is_square=is_square,
        parameters=parameters)

  def _shape(self):
    return self._matrix.shape

  def _shape_tensor(self):
    return tf.shape(self._matrix)

  def _matmul(self, x, adjoint=False, adjoint_arg=False):
    x = tf.convert_to_tensor(x, name="x")
    return tf.matmul(
        self._matrix, x, adjoint_a=adjoint, adjoint_b=adjoint_arg)

  def _solve(self, rhs, adjoint=False, adjoint_arg=False):
    rhs = tf.convert_to_tensor(rhs, name="rhs")
    assert not adjoint_arg, "Not implemented for this test class."
    return tf.linalg.solve(self._matrix, rhs, adjoint=adjoint)


@test_util.run_all_in_graph_and_eager_modes
class LinearOperatorTest(tf.test.TestCase):

  def test_all_shape_properties_defined_by_the_one_property_shape(self):

    shape = (1, 2, 3, 4)
    operator = LinearOperatorShape(shape)

    self.assertAllEqual(shape, operator.shape)
    self.assertAllEqual(4, operator.tensor_rank)
    self.assertAllEqual((1, 2), operator.batch_shape)
    self.assertAllEqual(4, operator.domain_dimension)
    self.assertAllEqual(3, operator.range_dimension)
    expected_parameters = {
        "is_non_singular": None,
        "is_positive_definite": None,
        "is_self_adjoint": None,
        "is_square": None,
        "shape": (1, 2, 3, 4),
    }
    self.assertEqual(expected_parameters, operator.parameters)

  def test_all_shape_methods_defined_by_the_one_method_shape(self):
    with self.cached_session():
      shape = (1, 2, 3, 4)
      operator = LinearOperatorShape(shape)

      self.assertAllEqual(shape, self.evaluate(operator.shape_tensor()))
      self.assertAllEqual(4, self.evaluate(operator.tensor_rank_tensor()))
      self.assertAllEqual((1, 2), self.evaluate(operator.batch_shape_tensor()))
      self.assertAllEqual(4, self.evaluate(operator.domain_dimension_tensor()))
      self.assertAllEqual(3, self.evaluate(operator.range_dimension_tensor()))

  def test_is_x_properties(self):
    operator = LinearOperatorShape(
        shape=(2, 2),
        is_non_singular=False,
        is_self_adjoint=True,
        is_positive_definite=False)
    self.assertFalse(operator.is_non_singular)
    self.assertTrue(operator.is_self_adjoint)
    self.assertFalse(operator.is_positive_definite)

  def test_nontrivial_parameters(self):
    matrix = rng.randn(2, 3, 4)
    matrix_ph = tf.compat.v1.placeholder_with_default(input=matrix, shape=None)
    operator = LinearOperatorMatmulSolve(matrix_ph)
    expected_parameters = {
        "is_non_singular": None,
        "is_positive_definite": None,
        "is_self_adjoint": None,
        "is_square": None,
        "matrix": matrix_ph,
    }
    self.assertEqual(expected_parameters, operator.parameters)

  def test_generic_to_dense_method_non_square_matrix_static(self):
    matrix = rng.randn(2, 3, 4)
    operator = LinearOperatorMatmulSolve(matrix)
    with self.cached_session():
      operator_dense = operator.to_dense()
      self.assertAllEqual((2, 3, 4), operator_dense.shape)
      self.assertAllClose(matrix, self.evaluate(operator_dense))

  def test_generic_to_dense_method_non_square_matrix_tensor(self):
    matrix = rng.randn(2, 3, 4)
    matrix_ph = tf.compat.v1.placeholder_with_default(input=matrix, shape=None)
    operator = LinearOperatorMatmulSolve(matrix_ph)
    operator_dense = operator.to_dense()
    self.assertAllClose(matrix, self.evaluate(operator_dense))

  def test_matvec(self):
    matrix = [[1., 0], [0., 2.]]
    operator = LinearOperatorMatmulSolve(matrix)
    x = [1., 1.]
    with self.cached_session():
      y = operator.matvec(x)
      self.assertAllEqual((2,), y.shape)
      self.assertAllClose([1., 2.], self.evaluate(y))

  def test_solvevec(self):
    matrix = [[1., 0], [0., 2.]]
    operator = LinearOperatorMatmulSolve(matrix)
    y = [1., 1.]
    with self.cached_session():
      x = operator.solvevec(y)
      self.assertAllEqual((2,), x.shape)
      self.assertAllClose([1., 1 / 2.], self.evaluate(x))

  def test_add(self):
    matrix = [[1., 0], [0., 2.]]
    operator = LinearOperatorMatmulSolve(matrix)
    with self.cached_session():
      y = operator.add(matrix)
      self.assertAllEqual((2, 2), y.shape)
      self.assertAllClose([[2., 0], [0., 4.]], self.evaluate(y))

  def test_is_square_set_to_true_for_square_static_shapes(self):
    operator = LinearOperatorShape(shape=(2, 4, 4))
    self.assertTrue(operator.is_square)

  def test_is_square_set_to_false_for_square_static_shapes(self):
    operator = LinearOperatorShape(shape=(2, 3, 4))
    self.assertFalse(operator.is_square)

  def test_is_square_set_incorrectly_to_false_raises(self):
    with self.assertRaisesRegex(ValueError, "but.*was square"):
      _ = LinearOperatorShape(shape=(2, 4, 4), is_square=False).is_square

  def test_is_square_set_inconsistent_with_other_hints_raises(self):
    with self.assertRaisesRegex(ValueError, "is always square"):
      matrix = tf.compat.v1.placeholder_with_default(input=(), shape=None)
      LinearOperatorMatmulSolve(matrix, is_non_singular=True, is_square=False)

    with self.assertRaisesRegex(ValueError, "is always square"):
      matrix = tf.compat.v1.placeholder_with_default(input=(), shape=None)
      LinearOperatorMatmulSolve(
          matrix, is_positive_definite=True, is_square=False)

  def test_non_square_operators_raise_on_determinant_and_solve(self):
    operator = LinearOperatorShape((2, 3))
    with self.assertRaisesRegex(NotImplementedError, "not be square"):
      operator.determinant()
    with self.assertRaisesRegex(NotImplementedError, "not be square"):
      operator.log_abs_determinant()
    with self.assertRaisesRegex(NotImplementedError, "not be square"):
      operator.solve(rng.rand(2, 2))

    with self.assertRaisesRegex(ValueError, "is always square"):
      matrix = tf.compat.v1.placeholder_with_default(input=(), shape=None)
      LinearOperatorMatmulSolve(
          matrix, is_positive_definite=True, is_square=False)

  def test_is_square_manual_set_works(self):
    matrix = tf.compat.v1.placeholder_with_default(
        input=np.ones((2, 2)), shape=None)
    operator = LinearOperatorMatmulSolve(matrix)
    if not tf.executing_eagerly():
      # Eager mode will read in the default value, and discover the answer is
      # True.  Graph mode must rely on the hint, since the placeholder has
      # shape=None...the hint is, by default, None.
      self.assertEqual(None, operator.is_square)

    # Set to True
    operator = LinearOperatorMatmulSolve(matrix, is_square=True)
    self.assertTrue(operator.is_square)

  def test_linear_operator_matmul_hints_closed(self):
    matrix = tf.compat.v1.placeholder_with_default(input=np.ones((2, 2)),
                                                   shape=None)
    operator1 = LinearOperatorMatmulSolve(matrix)

    operator_matmul = operator1.matmul(operator1)

    if not tf.executing_eagerly():
      # Eager mode will read in the input and discover matrix is square.
      self.assertEqual(None, operator_matmul.is_square)
    self.assertEqual(None, operator_matmul.is_non_singular)
    self.assertEqual(None, operator_matmul.is_self_adjoint)
    self.assertEqual(None, operator_matmul.is_positive_definite)

    operator2 = LinearOperatorMatmulSolve(
        matrix,
        is_non_singular=True,
        is_self_adjoint=True,
        is_positive_definite=True,
        is_square=True,
    )

    operator_matmul = operator2.matmul(operator2)

    self.assertTrue(operator_matmul.is_square)
    self.assertTrue(operator_matmul.is_non_singular)
    self.assertEqual(None, operator_matmul.is_self_adjoint)
    self.assertEqual(None, operator_matmul.is_positive_definite)

  def test_linear_operator_matmul_hints_false(self):
    matrix1 = tf.compat.v1.placeholder_with_default(
        input=rng.rand(2, 2), shape=None)
    operator1 = LinearOperatorMatmulSolve(
        matrix1,
        is_non_singular=False,
        is_self_adjoint=False,
        is_positive_definite=False,
        is_square=True,
    )

    operator_matmul = operator1.matmul(operator1)

    self.assertTrue(operator_matmul.is_square)
    self.assertFalse(operator_matmul.is_non_singular)
    self.assertEqual(None, operator_matmul.is_self_adjoint)
    self.assertEqual(None, operator_matmul.is_positive_definite)

    matrix2 = tf.compat.v1.placeholder_with_default(
        input=rng.rand(2, 3), shape=None)
    operator2 = LinearOperatorMatmulSolve(
        matrix2,
        is_non_singular=False,
        is_self_adjoint=False,
        is_positive_definite=False,
        is_square=False,
    )

    operator_matmul = operator2.matmul(operator2, adjoint_arg=True)

    if tf.executing_eagerly():
      self.assertTrue(operator_matmul.is_square)
      # False since we specified is_non_singular=False.
      self.assertFalse(operator_matmul.is_non_singular)
    else:
      self.assertIsNone(operator_matmul.is_square)
      # May be non-singular, since it's the composition of two non-square.
      # TODO(b/136162840) This is a bit inconsistent, and should probably be
      # False since we specified operator2.is_non_singular == False.
      self.assertIsNone(operator_matmul.is_non_singular)

    # No way to deduce these, even in Eager mode.
    self.assertIsNone(operator_matmul.is_self_adjoint)
    self.assertIsNone(operator_matmul.is_positive_definite)

  def test_linear_operator_matmul_hint_infer_square(self):
    matrix1 = tf.compat.v1.placeholder_with_default(
        input=rng.rand(2, 3), shape=(2, 3))
    matrix2 = tf.compat.v1.placeholder_with_default(
        input=rng.rand(3, 2), shape=(3, 2))
    matrix3 = tf.compat.v1.placeholder_with_default(
        input=rng.rand(3, 4), shape=(3, 4))

    operator1 = LinearOperatorMatmulSolve(matrix1, is_square=False)
    operator2 = LinearOperatorMatmulSolve(matrix2, is_square=False)
    operator3 = LinearOperatorMatmulSolve(matrix3, is_square=False)

    self.assertTrue(operator1.matmul(operator2).is_square)
    self.assertTrue(operator2.matmul(operator1).is_square)
    self.assertFalse(operator1.matmul(operator3).is_square)

  def testDispatchedMethods(self):
    operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        [[1., 0.5], [0.5, 1.]],
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    methods = {
        "trace": tf.linalg.trace,
        "diag_part": tf.linalg.diag_part,
        "log_abs_determinant": tf.linalg.logdet,
        "determinant": tf.linalg.det
    }
    for method in methods:
      op_val = getattr(operator, method)()
      linalg_val = methods[method](operator)
      self.assertAllClose(
          self.evaluate(op_val),
          self.evaluate(linalg_val))
    # Solve and Matmul go here.

    adjoint = tf.linalg.adjoint(operator)
    self.assertIsInstance(adjoint, linear_operator.LinearOperator)
    cholesky = tf.linalg.cholesky(operator)
    self.assertIsInstance(cholesky, linear_operator.LinearOperator)
    inverse = tf.linalg.inv(operator)
    self.assertIsInstance(inverse, linear_operator.LinearOperator)

  def testDispatchMatmulSolve(self):
    operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        np.float64([[1., 0.5], [0.5, 1.]]),
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    rhs = np.random.uniform(-1., 1., size=[3, 2, 2])
    for adjoint in [False, True]:
      for adjoint_arg in [False, True]:
        op_val = operator.matmul(
            rhs, adjoint=adjoint, adjoint_arg=adjoint_arg)
        matmul_val = tf.matmul(
            operator, rhs, adjoint_a=adjoint, adjoint_b=adjoint_arg)
        self.assertAllClose(
            self.evaluate(op_val), self.evaluate(matmul_val))

      op_val = operator.solve(rhs, adjoint=adjoint)
      solve_val = tf.linalg.solve(operator, rhs, adjoint=adjoint)
      self.assertAllClose(
          self.evaluate(op_val), self.evaluate(solve_val))

  def testDispatchMatmulLeftOperatorIsTensor(self):
    mat = np.float64([[1., 0.5], [0.5, 1.]])
    right_operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        mat,
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    lhs = np.random.uniform(-1., 1., size=[3, 2, 2])

    for adjoint in [False, True]:
      for adjoint_arg in [False, True]:
        op_val = tf.matmul(
            lhs, mat, adjoint_a=adjoint, adjoint_b=adjoint_arg)
        matmul_val = tf.matmul(
            lhs, right_operator, adjoint_a=adjoint, adjoint_b=adjoint_arg)
        self.assertAllClose(
            self.evaluate(op_val), self.evaluate(matmul_val))

  def testDispatchAdd(self):
    operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        np.float64([[1., 0.5], [0.5, 1.]]),
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    rhs = np.random.uniform(-1., 1., size=[3, 2, 2])
    op_val = operator.add(rhs)
    add_val = tf.math.add(operator, rhs)
    self.assertAllClose(self.evaluate(op_val), self.evaluate(add_val))

  def testDispatchMatmulLeftOperatorIsTensor(self):
    mat = np.float64([[1., 0.5], [0.5, 1.]])
    right_operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        mat,
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    lhs = np.random.uniform(-1., 1., size=[3, 2, 2])
    op_val = tf.math.add(lhs, mat)
    add_val = tf.math.add(lhs, right_operator)
    self.assertAllClose(self.evaluate(op_val), self.evaluate(add_val))

  def testDispatchAddOperator(self):
    operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
        np.float64([[1., 0.5], [0.5, 1.]]),
        is_square=True,
        is_self_adjoint=True,
        is_non_singular=True,
        is_positive_definite=True)
    rhs = np.random.uniform(-1., 1., size=[3, 2, 2])
    add_val = tf.math.add(operator, rhs)
    op_val = operator + rhs
    self.assertAllClose(self.evaluate(add_val), self.evaluate(op_val))

  def testVectorizedMap(self):

    def fn(x):
      y = tf.constant([3., 4.])
      # Make a [2, N, N] shaped operator.
      x = x * y[..., tf.compat.v1.newaxis, tf.compat.v1.newaxis]
      operator = linear_operator_full_matrix.LinearOperatorFullMatrix(
          x, is_square=True)
      return operator

    x = np.random.uniform(-1., 1., size=[3, 5, 5]).astype(np.float32)
    batched_operator = tf.vectorized_map(
        fn, tf.convert_to_tensor(x))
    self.assertIsInstance(batched_operator, linear_operator.LinearOperator)
    self.assertAllEqual(batched_operator.batch_shape, [3, 2])


if __name__ == "__main__":
  tf.test.main()
