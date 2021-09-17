# Copyright 2021 University College London. All Rights Reserved.
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
"""Convex operators."""

import abc
import contextlib

import tensorflow as tf
import tensorflow_probability as tfp

from tensorflow_mri.python.ops import linalg_ops


class ConvexOperator(object):
  """Base class defining a [batch of] convex operator[s]."""

  def __init__(self,
               dtype,
               name=None):
    """Initialize this `ConvexOperator`."""
    self._dtype = tf.dtypes.as_dtype(dtype)
    self._name = name or type(self).__name__

  def __call__(self, x):
    return self.call(x)

  def call(self, x, name="call"):
    """Apply this `ConvexOperator` to [batch] input `x`.
    
    Args:
      x: A `Tensor` with compatible shape and same `dtype` as `self`.
      name: A name for this operation.
    
    Returns:
      A `Tensor` with the same `dtype` as `self`.
    """
    with self._name_scope(name):
      x = tf.convert_to_tensor(x, name="x")
      self._check_input_dtype(x)
      return self._call(x)

  def prox(self, x, name="prox"):
    """Evaluate the proximal operator of this `ConvexOperator` at point `x`.

    Args:
      x: A `Tensor` with compatible shape and same `dtype` as `self`.
      name: A name for this operation. 

    Returns:
      A `Tensor` with the same `dtype` as `self`.
    """
    with self._name_scope(name):
      x = tf.convert_to_tensor(x, name="x")
      self._check_input_dtype(x)
      return self._prox(x)

  @abc.abstractmethod
  def _call(self, x):
    # Must be implemented by subclasses.
    raise NotImplementedError("Method `_call` is not implemented.")

  @abc.abstractmethod
  def _prox(self, x):
    # Must be implemented by subclasses.
    raise NotImplementedError("Method `_prox` is not implemented.")

  @property
  def dtype(self):
    """The `DType` of `Tensors` handled by this `ConvexOperator`."""
    return self._dtype

  @property
  def name(self):
    """Name prepended to all ops created by this `ConvexOperator`."""
    return self._name

  @contextlib.contextmanager
  def _name_scope(self, name=None):
    """Helper function to standardize op scope."""
    full_name = self.name
    if name is not None:
      full_name += "/" + name
    with tf.name_scope(full_name) as scope:
      yield scope

  def _check_input_dtype(self, arg):
    """Check that arg.dtype == self.dtype."""
    if arg.dtype.base_dtype != self.dtype:
      raise TypeError(
          "Expected argument to have dtype %s, but found: %s in tensor %s" %
          (self.dtype, arg.dtype, arg))


class ConvexOperatorL1Norm(ConvexOperator):
  """A `ConvexOperator` computing the L1-norm of a [batch of] inputs.

  Args:
    gamma: A `float`. A scaling factor. Defaults to 1.0.
    axis: An `int` or a list of `ints`. The dimensions to reduce. If `None`,
      reduces all dimensions. Defaults to `None`.
    dtype: A `string` or `DType`. The type of this operator. Defaults to
      `tf.float32`.
    name: A name for this operator.
  """
  def __init__(self,
               gamma=1.0,
               axis=None,
               dtype=tf.float32,
               name=None):

    super().__init__(dtype, name=name)
    self._gamma = gamma
    self._axis = axis

  def _call(self, x):
    return self._gamma * tf.math.reduce_sum(tf.math.abs(x), axis=self._axis)

  def _prox(self, x):
    return tfp.math.soft_threshold(x, self._gamma)


class Regularizer(object):
  """Base class defining a [batch of] regularizer[s]."""
  def __init__(self, convex_operator, linear_operator):
    """Initialize this `Regularizer`."""
    self._cvx_op = convex_operator
    self._lin_op = linear_operator

  def __call__(self, x):
    # Apply linear transformation, then convex operator.
    return self._cvx_op(tf.linalg.matvec(self._lin_op, x))
