# Copyright 2022 The TensorFlow MRI Authors. All Rights Reserved.
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
"""Fourier linear operator."""

import numpy as np
import tensorflow as tf

from tensorflow_mri.python.linalg import linear_operator
from tensorflow_mri.python.ops import fft_ops
from tensorflow_mri.python.util import api_util
from tensorflow_mri.python.util import tensor_util
from tensorflow_mri.python.util import types_util


@api_util.export("linalg.LinearOperatorFFT")
@linear_operator.make_composite_tensor
class LinearOperatorFFT(linear_operator.LinearOperator):
  r"""Linear operator acting like a [batch] DFT matrix.

  Args:
    domain_shape:  Shape of the domain of the operator.
  """
  def __init__(self,
               domain_shape,
               batch_shape=None,
               dtype=None,
               mask=None,
               is_non_singular=True,
               is_self_adjoint=False,
               is_positive_definite=None,
               is_square=True,
               name='LinearOperatorFFT'):

    parameters = dict(
        domain_shape=domain_shape,
        batch_shape=batch_shape,
        dtype=dtype,
        mask=mask,
        is_non_singular=is_non_singular,
        is_self_adjoint=is_self_adjoint,
        is_positive_definite=is_positive_definite,
        is_square=is_square,
        name=name)

    dtype = dtype or tf.complex64

    with tf.name_scope(name):
      dtype = tf.dtypes.as_dtype(dtype)
      if not is_non_singular:
        raise ValueError("An FFT operator is always non-singular.")
      if is_self_adjoint:
        raise ValueError("An FFT operator is never self-adjoint.")
      if not is_square:
        raise ValueError("An FFT operator is always square.")

      # Get static/dynamic domain shape.
      types_util.assert_not_ref_type(domain_shape, 'domain_shape')
      self._domain_shape_static, self._domain_shape_dynamic = (
          tensor_util.static_and_dynamic_shapes_from_shape(domain_shape))
      # Get static/dynamic batch shape.
      if batch_shape is not None:
        types_util.assert_not_ref_type(batch_shape, 'batch_shape')
        self._batch_shape_static, self._batch_shape_dynamic = (
            tensor_util.static_and_dynamic_shapes_from_shape(batch_shape))
      else:
        self._batch_shape_static = tf.TensorShape([])
        self._batch_shape_dynamic = tf.constant([], dtype=tf.int32)

      if mask is not None:
        self._mask = tf.convert_to_tensor(mask, dtype=tf.bool, name='mask')
        self._mask_mult = tf.cast(self._mask, dtype)
        self._mask_algo = 'multiply'

        rank_static = self._domain_shape_static.rank
        if rank_static is not None:
          mask_domain_shape = self._mask.shape[-rank_static:]
          if not self._domain_shape_static.is_compatible_with(
              mask_domain_shape):
            raise ValueError(
                f"The domain dimensions of mask {mask_domain_shape} must be "
                f"compatible with this operator's domain shape "
                f"{self._domain_shape_static}.")

          # Update batch shape.
          mask_batch_shape = self._mask.shape[:-rank_static]
          try:
            self._batch_shape_static = tf.broadcast_static_shape(
                self._batch_shape_static, mask_batch_shape)
          except ValueError:
            raise ValueError(
                f"The batch dimensions of mask {mask_batch_shape} must be "
                f"broadcastable with this operator's batch shape "
                f"{self._batch_shape_static}.")
          self._batch_shape_dynamic = tf.broadcast_dynamic_shape(
              self._batch_shape_dynamic, tf.shape(self._mask)[:-rank_static])

        else:
          # Need to use dynamic rank, and we can't figure out the static batch
          # shape.
          rank_dynamic = tf.size(self._domain_shape_dynamic)
          self._batch_shape_static = tf.TensorShape(None)
          self._batch_shape_dynamic = tf.broadcast_dynamic_shape(
              self._batch_shape_dynamic, tf.shape(self._mask)[:-rank_dynamic])

      else:
        self._mask = None

      super().__init__(dtype,
                       is_non_singular=is_non_singular,
                       is_self_adjoint=is_self_adjoint,
                       is_positive_definite=is_positive_definite,
                       is_square=is_square,
                       parameters=parameters,
                       name=name)

  def _matvec_nd(self, x, adjoint=False):
    if self.rank is not None:
      axes = list(range(-self.rank, 0))
    else:
      axes = tf.range(-self.rank_tensor(), 0)

    if adjoint:
      x = self._apply_mask(x)
      x = fft_ops.ifftn(x, axes=axes, norm='ortho', shift=True)

    else:
      x = fft_ops.fftn(x, axes=axes, norm='ortho', shift=True)
      x = self._apply_mask(x)

    # For consistent broadcasting semantics.
    if adjoint:
      output_shape = self.domain_shape_tensor()
    else:
      output_shape = self.range_shape_tensor()

    if self.batch_shape.rank is None or self.batch_shape.rank > 0:
      x = tf.broadcast_to(
          x, tf.concat([self.batch_shape_tensor(), output_shape], 0))

    return x

  def _solvevec_nd(self, rhs, adjoint=False):
    return self._matvec_nd(rhs, adjoint=(not adjoint))

  def _apply_mask(self, x):
    if self._mask is None:
      return x
    if self._mask_algo == 'multiply':
      x = x * self._mask_mult
    elif self._mask_algo == 'multiplex':
      x = tf.where(self._mask, x, tf.zeros_like(x))
    else:
      raise ValueError(f"Unknown masking algorithm: {self._mask_algo}")
    return x

  def _domain_shape(self):
    return self._domain_shape_static

  def _range_shape(self):
    return self._domain_shape_static

  def _batch_shape(self):
    return self._batch_shape_static

  def _domain_shape_tensor(self):
    return self._domain_shape_dynamic

  def _range_shape_tensor(self):
    return self._domain_shape_dynamic

  def _batch_shape_tensor(self):
    return self._batch_shape_dynamic

  @property
  def rank(self):
    return self.domain_shape.rank

  def rank_tensor(self):
    if self.rank is not None:  # Prefer static rank if available.
      return tf.convert_to_tensor(self.rank, dtype=tf.int32)
    return tf.size(self.domain_shape_tensor())

  def _prefer_static_rank(self):
    if self.rank is not None:
      return self.rank
    return self.rank_tensor()

  @property
  def _composite_tensor_fields(self):
    return ('domain_shape', 'batch_shape', 'dtype')

  @property
  def _composite_tensor_prefer_static_fields(self):
    return ("domain_shape", "batch_shape")

  @property
  def _experimental_parameter_ndims_to_matrix_ndims(self):
    return {}

  def __getitem__(self, slices):
    # Support slicing.
    new_batch_shape = tf.shape(tf.ones(self.batch_shape_tensor())[slices])
    parameters = dict(self.parameters, batch_shape=new_batch_shape)
    return LinearOperatorFFT(**parameters)


@api_util.export("linalg.dft_matrix")
def dft_matrix(num_rows,
               batch_shape=None,
               dtype=tf.complex64,
               shift=False,
               name=None):
  """Constructs a DFT matrix.

  Args:
    num_rows: A non-negative `int32` scalar `tf.Tensor` giving the number
      of rows in each batch matrix.
    batch_shape: A 1D integer `tf.Tensor`. If provided, the returned
      `tf.Tensor` will have leading batch dimensions of this shape.
    dtype: A `tf.dtypes.DType`. The type of an element in the resulting
      `tf.Tensor`.
    shift: A boolean. If `True`, returns the matrix for a DC-centred DFT.
    name: A name for this op.

  Returns:
    A `tf.Tensor` of shape `batch_shape + [num_rows, num_rows]` and type
    `dtype` containing a DFT matrix.
  """
  with tf.name_scope(name or "dft_matrix"):
    num_rows = tf.convert_to_tensor(num_rows)
    if batch_shape is not None:
      batch_shape = tensor_util.convert_shape_to_tensor(batch_shape)
    dtype = tf.dtypes.as_dtype(dtype)

    i = tf.range(num_rows, dtype=dtype.real_dtype)
    omegas = tf.reshape(
        tf.math.exp(tf.dtypes.complex(
            tf.constant(0.0, dtype=dtype.real_dtype),
            -2.0 * np.pi * i / tf.cast(num_rows, dtype.real_dtype))), [-1, 1])
    m = omegas ** tf.cast(i, dtype)
    m /= tf.math.sqrt(tf.cast(num_rows, dtype))

    if shift:
      m = tf.signal.fftshift(m)

    if batch_shape is not None:
      m = tf.broadcast_to(m, tf.concat([batch_shape, [num_rows, num_rows]], 0))

    return m
