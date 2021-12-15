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
"""Linear algebra operations.

This module contains linear algebra operators and solvers.
"""

import abc
import collections

import tensorflow as tf
import tensorflow_nufft as tfft

from tensorflow_mri.python.ops import fft_ops
from tensorflow_mri.python.util import check_util
from tensorflow_mri.python.util import tensor_util


class LinearOperatorImaging(tf.linalg.LinearOperator):
  """Linear operator meant to operate on images.

  This `LinearOperator` defines some additional methods to simplify operations
  on images, while maintaining compatibility with the TensorFlow linear algebra
  framework.

  Mainly this operator does two things:

  * Acknowledge the fact that its inputs and outputs may have meaningful shapes,
    although they must of course be vectorized before matrix-matrix and
    matrix-vector multiplications. To this end it defines the additional
    properties `domain_shape` and `range_shape`. These enrich the information
    provided by built-in `shape`, `domain_dimension` and `range_dimension`,
    though note that they cannot perform any checks since inputs and outputs are
    always vectorized. The built-in `shape` is overriden as it can be computed
    from `domain_shape` and `range_shape`.
  * Acknowledge the fact that this operator will operate on vectorized images
    and so it will perform primarily matrix-vector multiplications. Therefore
    the requirement to implement matrix-matrix multiplication is removed.
    Nevertheless this operator must still be able to accept matrix arguments to
    be compatible with the TensorFlow linear algebra framework. Thus `matmul`
    is overriden to call `matvec` if the outer dimension of the argument is 1
    and raise an error otherwise.

  To summarize, subclasses of `LinearOperatorImaging` MUST:

  * Override `_domain_shape` and `_range_shape`.
  * Override `_matvec`.

  Optionally, subclasses may also:

  * Override `_batch_shape`, if the operator has a batch shape (default is a
    scalar batch shape).
  * Override `_domain_shape_tensor`, `_range_shape_tensor` and
    `_batch_shape_tensor` to provide dynamically determined domain and range
    shapes.

  Generally, operators should NOT need to:

  * Override `_shape`, this is provided for free.
  * Override `_matmul`, this is provided for free.

  For the parameters, see `tf.linalg.LinearOperator`.
  """
  @property
  def domain_shape(self):
    """Domain shape of this linear operator."""
    return self._domain_shape()

  @property
  def range_shape(self):
    """Range shape of this linear operator."""
    return self._range_shape()

  @property
  def domain_rank(self):
    return self.domain_shape.rank
  
  @property
  def range_rank(self):
    return self.range_shape.rank

  @property
  def rank(self):
    """Rank (in the sense of spatial dimensionality) of this linear operator."""
    return self._rank()

  def domain_shape_tensor(self, name="domain_shape_tensor"):
    """Domain shape of this linear operator, determined at runtime."""
    with self._name_scope(name): # pylint: disable=not-callable
      # Prefer to use statically defined shape if available.
      if self.domain_shape.is_fully_defined():
        return tensor_util.convert_shape_to_tensor(self.domain_shape.as_list())
      return self._domain_shape_tensor()

  def range_shape_tensor(self, name="range_shape_tensor"):
    """Range shape of this linear operator, determined at runtime."""
    with self._name_scope(name): # pylint: disable=not-callable
      # Prefer to use statically defined shape if available.
      if self.range_shape.is_fully_defined():
        return tensor_util.convert_shape_to_tensor(self.range_shape.as_list())
      return self._range_shape_tensor()

  def batch_shape_tensor(self, name="batch_shape_tensor"):
    """Batch shape of this linear operator, determined at runtime."""
    with self._name_scope(name): # pylint: disable=not-callable
      if self.batch_shape.is_fully_defined():
        return tensor_util.convert_shape_to_tensor(self.batch_shape.as_list())
      return self._batch_shape_tensor()

  @abc.abstractmethod
  def _domain_shape(self):
    # Users must override this method.
    return tf.TensorShape(None)

  @abc.abstractmethod
  def _range_shape(self):
    # Users must override this method.
    return tf.TensorShape(None)

  def _batch_shape(self):
    # Users should override this method if this operator has a batch shape.
    return tf.TensorShape([])

  def _rank(self):
    return self.domain_shape.rank

  def _domain_shape_tensor(self):
    raise NotImplementedError("_domain_shape_tensor is not implemented.")

  def _range_shape_tensor(self):
    raise NotImplementedError("_range_shape_tensor is not implemented.")

  def _batch_shape_tensor(self, shape=None):
    return tf.constant([], dtype=tf.int32)

  def _shape(self):
    # Default implementation of `_shape` for imaging operators.
    return self._batch_shape() + tf.TensorShape(
        [self.range_shape.num_elements(),
         self.domain_shape.num_elements()])

  def _shape_tensor(self):
    # Default implementation of `_shape_tensor` for imaging operators.
    return tf.concat([self.batch_shape_tensor(),
                      [tf.size(self.range_shape_tensor()),
                       tf.size(self.domain_shape_tensor())]], 0)

  def _matmul(self, x, adjoint=False, adjoint_arg=False):
    # Default implementation of `matmul` for imaging operator. If outer
    # dimension of argument is 1, call `matvec`. Otherwise raise an error.
    arg_outer_dim = -2 if adjoint_arg else -1

    if x.shape[arg_outer_dim] != 1:
      raise ValueError(
        f"`{self.__class__.__name__}` does not support matrix multiplication.")

    x = tf.squeeze(x, axis=arg_outer_dim)
    x = self.matvec(x, adjoint=adjoint)
    x = tf.expand_dims(x, axis=arg_outer_dim)
    return x

  def _matvec(self, x, adjoint=False):
    # Default implementation of `_matvec` for imaging operator.
    x = self.expand_output(x) if adjoint else self.expand_input(x) 
    x = self._transform(x, adjoint=adjoint)
    x = self.flatten_input(x) if adjoint else self.flatten_output(x) 
    return x

  @abc.abstractmethod
  def _transform(self, x, adjoint=False):
    # Subclasses must override this method.
    raise NotImplementedError("Method `_transform` is not implemented.")

  def adjoint(self, name="adjoint"):
    if self.is_self_adjoint:
      return self
    with self._name_scope(name):
      return LinearOperatorImagingAdjoint(self)

  H = property(adjoint, None)

  def flatten_input(self, x):
    self.domain_shape.assert_is_compatible_with(x.shape[-self.domain_rank:])

    batch_shape = x.shape[:-self.domain_rank]
    batch_shape_tensor = tf.shape(x)[:-self.domain_rank]

    output_shape = batch_shape + self.domain_dimension
    output_shape_tensor = tf.concat(
        [batch_shape_tensor, [self.domain_dimension_tensor()]], axis=0)

    x = tf.reshape(x, output_shape_tensor)      
    return tf.ensure_shape(x, output_shape)

  def expand_input(self, x):
    self.domain_dimension.assert_is_compatible_with(x.shape[-1])

    batch_shape = x.shape[:-1]
    batch_shape_tensor = tf.shape(x)[:-1]

    output_shape = batch_shape + self.domain_shape
    output_shape_tensor = tf.concat([
        batch_shape_tensor, self.domain_shape_tensor()], axis=0)

    x = tf.reshape(x, output_shape_tensor)
    return tf.ensure_shape(x, output_shape)

  def flatten_output(self, x):
    self.range_shape.assert_is_compatible_with(x.shape[-self.range_rank:])

    batch_shape = x.shape[:-self.range_rank]
    batch_shape_tensor = tf.shape(x)[:-self.range_rank]

    output_shape = batch_shape + self.range_dimension
    output_shape_tensor = tf.concat(
        [batch_shape_tensor, [self.range_dimension_tensor()]], axis=0)

    x = tf.reshape(x, output_shape_tensor)
    return tf.ensure_shape(x, output_shape)

  def expand_output(self, x):
    self.range_dimension.assert_is_compatible_with(x.shape[-1])

    batch_shape = x.shape[:-1]
    batch_shape_tensor = tf.shape(x)[:-1]

    output_shape = batch_shape + self.range_shape
    output_shape_tensor = tf.concat([
        batch_shape_tensor, self.range_shape_tensor()], axis=0)

    x = tf.reshape(x, output_shape_tensor)
    return tf.ensure_shape(x, output_shape)
    

class LinearOperatorImagingAdjoint(tf.linalg.LinearOperatorAdjoint,
                                   LinearOperatorImaging):

  def _domain_shape(self):
    return self.operator.range_shape

  def _range_shape(self):
    return self.operator.domain_shape
  
  def _domain_shape_tensor(self):
    return self.operator.range_shape_tensor()

  def _range_shape_tensor(self):
    return self.operator.domain_shape_tensor()

  def _transform(self, x, adjoint=False):
    return self.operator._transform(x, adjoint=(not adjoint))


class LinearOperatorImagingComposition(tf.linalg.LinearOperatorComposition,
                                       LinearOperatorImaging):

  def _domain_shape(self):
    return self.operators[-1].domain_shape

  def _range_shape(self):
    return self.operators[0].range_shape
  
  def _domain_shape_tensor(self):
    return self.operators[-1].domain_shape_tensor()

  def _range_shape_tensor(self):
    return self.operators[0].range_shape_tensor()

  def _transform(self, x, adjoint=False, adjoint_arg=False):
    if adjoint:
      transform_order_list = self.operators
    else:
      transform_order_list = list(reversed(self.operators))

    result = transform_order_list[0]._transform(x, adjoint=adjoint)
    for operator in transform_order_list[1:]:
      result = operator._transform(result, adjoint=adjoint)
    return result

class LinearOperatorFFT(LinearOperatorImaging): # pylint: disable=abstract-method
  """Linear operator acting like a DFT matrix.

  This linear operator computes the N-dimensional discrete Fourier transform
  (DFT) of its inputs. In other words, it acts like a DFT matrix, although the
  corresponding dense matrix is never created. Instead, the computation is
  performed using the fast Fourier transform (FFT) algorithm.

  The adjoint of this operator computes the inverse discrete Fourier transform.

  This operator can act like an undersampled DFT operator if a boolean sampling
  mask is provided. In this case, the values for which the mask is `False` are
  set to 0 in the output. In the adjoint operator, the values for which mask is
  `False` are set to 0 in the input and do not contribute to the computation of
  the transform.

  Args:
    domain_shape: A `TensorShape` or list of ints. The domain shape of this
      operator. By default, the Fourier transform is computed along each of the
      dimensions of `domain_shape`. However, `domain_shape` may contain
      additional dimensions which should not be transformed. In this case, make
      sure to specify the `rank` argument, which should be equal to the
      dimensionality of the transform. For example, for a 12-coil 256 x 256
      transform, set `domain_shape=[12, 256, 256]` and `rank=2`.
    rank: An optional `int`. The rank (spatial dimensionality) of the transform.
      If `None`, defaults to `domain_shape.rank`. Must be 1, 2 or 3. Make sure
      to specify `rank` if `domain_shape` contains any additional dimensions
      which should not be transformed.
    mask: An optional `Tensor` of type `bool`. The sampling mask.
    norm: FFT normalization mode. Must be `None` (no normalization for either
      forward or adjoint operator) or `'ortho'` (normalization by the square
      root of the number of pixels in both the forward and adjoint operators).
      Defaults to `'ortho'`.
    dtype: An optional `string` or `DType`. The data type for this operator.
      Must be `complex64` or `complex128`. Defaults to `complex64`.
    name: An optional `string`. A name for this operator.
  """
  def __init__(self,
               domain_shape,
               rank=None,
               mask=None,
               norm='ortho',
               dtype=tf.dtypes.complex64,
               name="LinearOperatorFFT"):

    parameters = dict(
      domain_shape=domain_shape,
      rank=rank,
      mask=mask,
      norm=norm,
      dtype=dtype,
      name=name
    )

    # Set domain shape.
    self._domain_shape_value = tf.TensorShape(domain_shape)
    # Get rank from `rank` argument if provided, else infer from `domain_shape`.
    self._rank = rank or self._domain_shape_value.rank
    # Validate normalization mode.
    self._norm = check_util.validate_enum(norm, {None, 'ortho'}, 'norm')

    # Get the FFT axes.
    self._fft_axes = list(range(-self.rank, 0))

    if mask is None:
      # Set members to `None`.
      self._mask = None
      self._mask_complex = None
      # If no mask, this operator has no batch shape.
      self._batch_shape_value = tf.TensorShape([])
    else:
      # If mask was provided, convert to tensor and cast to this operator's dtype.
      self._mask = tf.convert_to_tensor(mask, dtype=tf.bool)
      self._mask_complex = tf.cast(self.mask, dtype)
      # Batch shape is any leading dimensions of `mask` not included in
      # `domain_shape`.
      self._batch_shape_value = self._mask.shape[:-self._domain_shape_value.rank]
      # Static check: last dimensions of `mask` must be broadcastable with
      # domain shape.
      try:
        _ = tf.broadcast_static_shape(
            self.mask.shape[-self._domain_shape_value.rank:], self.domain_shape)
      except ValueError as e:
        raise ValueError(
            "The last dimensions of `mask` must be broadcastable with "
            "`domain_shape`.") from e

    super().__init__(dtype,
                     is_non_singular=None,
                     is_self_adjoint=None,
                     is_positive_definite=None,
                     is_square=True,
                     name=name,
                     parameters=parameters)

  def _transform(self, x, adjoint=False):

    # If `norm` is `None`, we do not do normalization. In this case `norm` is
    # set to `forward` for IFFT and `backward` for the FFT, so that the
    # normalization is never applied.
    if adjoint:
      if self.mask is not None:
        x *= self._mask_complex
      x = fft_ops.ifftn(x, axes=self._fft_axes,
                        norm=self.norm or 'forward', shift=True)
    else:
      x = fft_ops.fftn(x, axes=self._fft_axes,
                       norm=self.norm or 'backward', shift=True)
      if self.mask is not None:
        x *= self._mask_complex
    return x

  def _domain_shape(self):
    return self._domain_shape_value

  def _range_shape(self):
    return self.domain_shape

  def _batch_shape(self):
    return self._batch_shape_value

  @property
  def rank(self):
    """Rank (in the sense of spatial dimensionality) of this linear operator."""
    return self._rank

  @property
  def mask(self):
    """Sampling mask."""
    return self._mask

  @property
  def norm(self):
    """Normalization mode."""
    return self._norm


class LinearOperatorNUFFT(LinearOperatorImaging): # pylint: disable=abstract-method
  """Linear operator acting like a nonuniform DFT matrix.

  Args:
    domain_shape: A `TensorShape` or a list of `ints`. The domain shape of this
      operator. This is usually the shape of the image but may include
      additional dimensions.
    points: A `Tensor`. Must have type `float32` or `float64`. Must have shape
      `[..., M, N]`, where `N` is the rank (or spatial dimensionality), `M` is
      the number of samples and `...` is the batch shape, which can have any
      number of dimensions.
    norm: FFT normalization mode. Must be `None` (no normalization) or
      `'ortho'`. Defaults to `'ortho'`.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               domain_shape,
               points,
               norm='ortho',
               name="LinearOperatorNUFFT"):

    parameters = dict(
      domain_shape=domain_shape,
      points=points,
      norm=norm,
      name=name
    )

    self._domain_shape_value = tf.TensorShape(domain_shape)
    self._points = check_util.validate_tensor_dtype(
      tf.convert_to_tensor(points), 'floating', 'points')
    self._rank = self._points.shape[-1]
    self._norm = check_util.validate_enum(norm, {None, 'ortho'}, 'norm')

    # Compute NUFFT batch shape. The NUFFT batch shape is different from this
    # operator's batch shape, and it is included in the operator's inner shape.
    nufft_batch_shape = self.domain_shape[:-self.rank]

    # Batch shape of `points` might have two parts: one that goes into NUFFT
    # batch shape and another that goes into this operator's batch shape.
    points_batch_shape = self.points.shape[:-2]

    if nufft_batch_shape.rank == 0:
      self._batch_shape_value = points_batch_shape
    else:
      # Take operator part of batch shape, then keep remainder.
      self._batch_shape_value = points_batch_shape[:-nufft_batch_shape.rank] # pylint: disable=invalid-unary-operand-type
      points_batch_shape = points_batch_shape[-nufft_batch_shape.rank:] # pylint: disable=invalid-unary-operand-type
      # Check that NUFFT part of points batch shape is broadcast compatible with
      # NUFFT batch shape.
      points_batch_shape = points_batch_shape.as_list()
      points_batch_shape = [None if s == 1 else s for s in points_batch_shape]
      points_batch_shape = [None] * (
        nufft_batch_shape.rank - len(points_batch_shape)) + points_batch_shape
      if not nufft_batch_shape.is_compatible_with(points_batch_shape):
        raise ValueError(
            f"The batch shape of `points` must be broadcastable to the batch "
            f"part of `domain_shape`. Received batch shapes "
            f"{str(self.domain_shape[:-self.rank])} and "
            f"{str(self.points.shape[:-2])} for input and `points`, "
            f"respectively.")
    self._range_shape_value = nufft_batch_shape + self.points.shape[-2:-1]

    is_square = self.domain_dimension == self.range_dimension

    super().__init__(tensor_util.get_complex_dtype(self.points.dtype),
                     is_non_singular=None,
                     is_self_adjoint=None,
                     is_positive_definite=None,
                     is_square=is_square,
                     name=name,
                     parameters=parameters)

    # Compute normalization factors.
    if self.norm == 'ortho':
      norm_factor = tf.math.reciprocal(
          tf.math.sqrt(tf.cast(self.grid_shape.num_elements(), self.dtype)))
      self._norm_factor_forward = norm_factor
      self._norm_factor_adjoint = norm_factor

  def _transform(self, x, adjoint=False):

    if adjoint:
      x = tfft.nufft(x, self.points,
                     grid_shape=self.grid_shape,
                     transform_type='type_1',
                     fft_direction='backward')
      if self.norm is not None:
        x *= self._norm_factor_adjoint
    else:
      x = tfft.nufft(x, self.points,
                     transform_type='type_2',
                     fft_direction='forward')
      if self.norm is not None:
        x *= self._norm_factor_forward
    return x

  def _domain_shape(self):
    return self._domain_shape_value

  def _range_shape(self):
    return self._range_shape_value

  def _batch_shape(self):
    return self._batch_shape_value

  @property
  def rank(self):
    """Rank (in the sense of spatial dimensionality) of this operator.
    The number of spatial dimensions in the images that this operator acts on.
    Returns:
      An `int`.
    """
    return self._rank

  @property
  def points(self):
    """Sampling coordinates.
    The set of nonuniform points in which this operator evaluates the Fourier
    transform.
    Returns:
      A `Tensor` of shape `[..., M, N]`.
    """
    return self._points

  @property
  def grid_shape(self):
    return self.domain_shape[-self.rank:]

  @property
  def norm(self):
    return self._norm


class LinearOperatorInterp(LinearOperatorNUFFT): # pylint: disable=abstract-method
  """Linear operator acting like an interpolator.

  Args:
    domain_shape: A `TensorShape` or a list of `ints`. The domain shape of this
      operator. This is usually the shape of the image but may include
      additional dimensions.
    points: A `Tensor`. Must have type `float32` or `float64`. Must have shape
      `[..., M, N]`, where `N` is the rank (or spatial dimensionality), `M` is
      the number of samples and `...` is the batch shape, which can have any
      number of dimensions.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               domain_shape,
               points,
               name="LinearOperatorInterp"):

    super().__init__(domain_shape, points, name=name)

  def _transform(self, x, adjoint=False):

    if adjoint:
      x = tfft.spread(x, self.points,
                      grid_shape=self.domain_shape[-self.rank:])
    else:
      x = tfft.interp(x, self.points)
    return x


class LinearOperatorSensitivityModulation(LinearOperatorImaging): # pylint: disable=abstract-method
  """Linear operator acting like a sensitivity modulation matrix.

  Args:
    sensitivities: A `Tensor`. The coil sensitivity maps. Must have type
      `complex64` or `complex128`. Must have shape `[..., C, *S]`, where `S`
      is the spatial shape, `C` is the number of coils and `...` is the batch
      shape, which can have any dimensionality. Note that `rank` must be
      specified if you intend to provide any batch dimensions.
    rank: An optional `int`. The rank (in the sense of spatial dimensionality)
      of this operator. Defaults to `sensitivities.shape.rank - 1`. Therefore,
      if `rank` is not specified, axis 0 is interpreted to be the coil axis
      and the remaining dimensions are interpreted to be spatial dimensions.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               sensitivities,
               rank=None,
               name='LinearOperatorSensitivityModulation'):

    parameters = dict(
      sensitivities=sensitivities,
      rank=rank,
      name=name
    )

    self._sensitivities = check_util.validate_tensor_dtype(
        tf.convert_to_tensor(sensitivities), 'complex', name='sensitivities')

    self._rank = rank or self.sensitivities.shape.rank - 1
    self._image_shape = self.sensitivities.shape[-self.rank:]
    self._num_coils = self.sensitivities.shape[-self.rank-1]
    self._coil_axis = -self.rank-1

    self._domain_shape_value = self.image_shape
    self._range_shape_value = tf.TensorShape([self.num_coils]) + self.image_shape
    self._batch_shape_value = self.sensitivities.shape[:-self.rank-1]

    super().__init__(self._sensitivities.dtype,
                     is_non_singular=False,
                     is_self_adjoint=False,
                     is_positive_definite=False,
                     is_square=False,
                     name=name,
                     parameters=parameters)

  def _domain_shape(self):
    return self._domain_shape_value

  def _range_shape(self):
    return self._range_shape_value

  def _batch_shape(self):
    return self._batch_shape_value

  def _transform(self, x, adjoint=False):

    if adjoint:
      x *= tf.math.conj(self.sensitivities)
      x = tf.math.reduce_sum(x, axis=self._coil_axis)
    else:
      x = tf.expand_dims(x, -self.domain_shape.rank-1)
      x *= self.sensitivities
    return x

  @property
  def rank(self):
    """Rank (in the sense of spatial dimensionality) of this operator.

    The number of spatial dimensions in the images that this operator acts on.

    Returns:
      An `int`.
    """
    return self._rank

  @property
  def image_shape(self):
    """Image shape.

    The shape of the images that this operator acts on.

    Returns:
      A `TensorShape`.
    """
    return self._image_shape

  @property
  def num_coils(self):
    """Number of coils.

    The number of coils in the arrays that this operator acts on.

    Returns:
      An `int`.
    """
    return self._num_coils

  @property
  def sensitivities(self):
    """Coil sensitivity maps.

    The coil sensitivity maps used by this linear operator.

    Returns:
      A `Tensor` of type `self.dtype`.
    """
    return self._sensitivities


class LinearOperatorParallelMRI(LinearOperatorImagingComposition): # pylint: disable=abstract-method
  """Linear operator acting like a parallel MRI matrix.

  Can be used for Cartesian or non-Cartesian imaging. The operator is
  non-Cartesian if the `trajectory` argument is passed, and Cartesian otherwise.

  Args:
    sensitivities: A `Tensor`. The coil sensitivity maps. Must have type
      `complex64` or `complex128`. Must have shape `[..., C, *S]`, where `S`
      is the spatial shape, `C` is the number of coils and `...` is the batch
      shape, which can have any dimensionality. Note that `rank` must be
      specified if you intend to provide any batch dimensions.
    mask: An optional `Tensor` of type `bool`. The sampling mask. Only relevant
      for Cartesian imaging.
    trajectory: An optional `Tensor`. Must have type `float32` or `float64`.
      Must have shape `[..., M, N]`, where `N` is the rank (or spatial
      dimensionality), `M` is the number of samples and `...` is the batch
      shape, which can have any number of dimensions and must be
      broadcast-compatible with the batch shape of `sensitivities`. If
      `trajectory` is provided, this operator is a non-Cartesian MRI operator.
      Otherwise, this is operator is a Cartesian MRI operator.
    rank: An optional `int`. The rank (in the sense of spatial dimensionality)
      of this operator. If `None`, the rank of this operator is inferred from
      the rank of `mask` / `trajectory` (as `mask.shape.rank`) if it is
      Cartesian, or from the static shape of `trajectory` (as
      `trajectory.shape[-1]`) if non-Cartesian. If this fails or `mask` and/or
      `trajectory` are `None`, the rank is inferred from `sensitivities` as
      `sensitivities.shape.rank - 1`. `rank` should be specified if there are
      any batch dimensions, as the inferred rank might be incorrect in this
      case.
    norm: FFT normalization mode. Must be `None` (no normalization) or
      `'ortho'`. Defaults to 'ortho'.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               sensitivities,
               mask=None,
               trajectory=None,
               rank=None,
               norm='ortho',
               name='LinearOperatorParallelMRI'):

    sensitivities = tf.convert_to_tensor(sensitivities)
    self._rank = rank

    if trajectory is not None:
      self._is_cartesian = False
      # Infer rank.
      self._rank = self._rank or trajectory.shape[-1]
      self._rank = self._rank or sensitivities.shape.rank - 1
      if self._rank is None:
        raise ValueError(
            "Could not infer the rank of the operator. Please provide the "
            "`rank` argument.")
      # Validate trajectory.
      trajectory = tf.convert_to_tensor(trajectory)
      trajectory = check_util.validate_tensor_dtype(
          trajectory, sensitivities.dtype.real_dtype, name='trajectory')
      trajectory = tf.expand_dims(trajectory, -3) # Add coil dimension.

    elif mask is not None:
      self._is_cartesian = True
      # Infer rank.
      self._rank = self._rank or mask.shape.rank
      self._rank = self._rank or sensitivities.shape.rank - 1
      if self._rank is None:
        raise ValueError(
            "Could not infer the rank of the operator. Please provide the "
            "`rank` argument.")
      # Validate mask.
      mask = tf.convert_to_tensor(mask)
      mask = check_util.validate_tensor_dtype(
          mask, tf.dtypes.bool, name='mask')
      mask = tf.expand_dims(mask, -self._rank-1) # Add coil dimension.

    else:
      raise ValueError("Either `mask` or `trajectory` must be provided.")

    # Prepare the Fourier operator.
    if trajectory is not None: # Non-Cartesian      
      linop_fourier = LinearOperatorNUFFT(
          sensitivities.shape[-self.rank-1:], trajectory, norm=norm) # pylint: disable=invalid-unary-operand-type
    else: # Cartesian      
      linop_fourier = LinearOperatorFFT(
          sensitivities.shape[-self.rank-1:], rank=rank, mask=mask, norm=norm) # pylint: disable=abstract-class-instantiated,invalid-unary-operand-type

    # Prepare the coil sensitivity operator.
    linop_sens = LinearOperatorSensitivityModulation(
      sensitivities, rank=self.rank)

    super().__init__([linop_fourier, linop_sens], name=name)

  @property
  def rank(self):
    """Rank (in the sense of spatial dimensionality) of this operator.

    The number of spatial dimensions in the images that this operator acts on.

    Returns:
      An `int`.
    """
    return self._rank

  @property
  def image_shape(self):
    """Image shape.

    The shape of the images that this operator acts on.

    Returns:
      A `TensorShape`.
    """
    return self.linop_sens.image_shape

  @property
  def num_coils(self):
    """Number of coils.

    The number of coils in the arrays that this operator acts on.

    Returns:
      An `int`.
    """
    return self.linop_sens.num_coils

  @property
  def is_cartesian(self):
    """Whether this is a Cartesian MRI operator.

    Returns `True` if this operator acts on a Cartesian *k*-space.

    Returns:
      A `bool`.
    """
    return self._is_cartesian

  @property
  def is_non_cartesian(self):
    """Whether this is a non-Cartesian MRI operator.

    Returns `True` if this operator acts on a non-Cartesian *k*-space.

    Returns:
      A `bool`.
    """
    return not self._is_cartesian

  @property
  def sensitivities(self):
    """Coil sensitivity maps.

    The coil sensitivity maps used by this linear operator.

    Returns:
      A `Tensor` of type `self.dtype`.
    """
    return self.linop_sens.sensitivities

  @property
  def trajectory(self):
    """*k*-space trajectory.

    The *k*-space trajectory used by this linear operator. Only valid if
    this operator acts on a non-Cartesian *k*-space.

    Returns:
      A `Tensor` if `self.is_non_cartesian` is `True`, `None` otherwise.
    """
    return self.linop_fourier.points if self.is_non_cartesian else None

  @property
  def linop_fourier(self):
    """Fourier linear operator.

    The Fourier operator used by this linear operator.

    Returns:
      A `LinearOperatorFFT` is `self.is_cartesian` is `True`, or a
      `LinearOperatorNUFFT` if `self.is_non_cartesian` is `True`.
    """
    return self.operators[0]

  @property
  def linop_sens(self):
    """Sensitivity modulation linear operator.

    The sensitivity modulation operator used by this linear operator.

    Returns:
      A `LinearOperatorSensitivityModulation`.
    """
    return self.operators[1]


class LinearOperatorRealWeighting(LinearOperatorImaging): # pylint: disable=abstract-method
  """Linear operator acting like a real weighting matrix.

  This is a square, self-adjoint operator.

  This operator acts like a diagonal matrix. It does not inherit from
  `LinearOperatorDiag` for efficiency reasons, as the diagonal values may be
  repeated periodically.

  Args:
    weights: A `Tensor`. Must have type `float32` or `float64`.
    arg_shape: A `TensorShape` or a list of `ints`. The domain/range shape.
    dtype: A `DType`. The data type for this operator. Defaults to
      `weights.dtype`.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               weights,
               arg_shape=None,
               dtype=None,
               name='LinearOperatorRealWeighting'):

    parameters = dict(
      weights=weights,
      arg_shape=arg_shape,
      dtype=dtype,
      name=name
    )

    # Only real floating-point types allowed.
    self._weights = check_util.validate_tensor_dtype(
      tf.convert_to_tensor(weights), 'floating', 'weights')

    # If a dtype was specified, cast weights to it.
    if dtype is not None:
      self._weights = tf.cast(self._weights, dtype)

    if arg_shape is None:
      self._domain_shape_value = self._weights.shape
    else:
      self._domain_shape_value = tf.TensorShape(arg_shape)
      # Check that the last dimensions of `shape.weights` are broadcastable to
      # this shape.
      weights_shape = self.weights.shape[-self.domain_shape.rank:]
      weights_shape = [None if s == 1 else s for s in weights_shape]
      # weights_shape = [None] * (self.domain_shape.rank - len(weights_shape)) + weights_shape
      if not self.domain_shape.is_compatible_with(weights_shape):
        raise ValueError(
          f"`weights.shape` must be broadcast compatible with `arg_shape`. "
          f"Received shapes {str(weights_shape)} and "
          f"{str(self.domain_shape)}, respectively.")

    self._range_shape_value = self.domain_shape
    self._batch_shape_value = self.weights.shape[:-self.domain_shape.rank]

    # This operator acts like a diagonal matrix. It does not inherit from
    # `LinearOperatorDiag` for efficiency reasons, as the diagonal values may
    # be repeated periodically.
    super().__init__(self._weights.dtype,
                     is_non_singular=None,
                     is_self_adjoint=True,
                     is_positive_definite=None,
                     is_square=True,
                     name=name,
                     parameters=parameters)

  def _domain_shape(self):
    return self._domain_shape_value

  def _range_shape(self):
    return self._range_shape_value

  def _batch_shape(self):
    return self._batch_shape_value

  def _transform(self, x, adjoint=False):
    x *= self.weights
    return x

  @property
  def weights(self):
    return self._weights


class LinearOperatorDifference(LinearOperatorImaging): # pylint: disable=abstract-method
  """Linear operator acting like a finite differences matrix.

  Args:
    domain_shape: A `tf.TensorShape` or list of ints. The domain shape of this
      operator.
    axis: An optional `int`. The axis along which the difference is taken.
      Defaults to -1.
    dtype: An optional `string` or `DType`. The data type for this operator.
      Defaults to `float32`.
    name: An optional `string`. A name for this operator.
  """
  def __init__(self,
               domain_shape,
               axis=-1,
               dtype=tf.dtypes.float32,
               name="LinearOperatorDifference"):

    parameters = dict(
      domain_shape=domain_shape,
      axis=axis,
      dtype=dtype,
      name=name
    )

    domain_shape = tf.TensorShape(domain_shape)

    self._scalar_axis = isinstance(axis, int)
    self._axis = check_util.validate_axis(axis, domain_shape.rank,
                                          max_length=1,
                                          canonicalize="negative",
                                          scalar_to_list=False)

    range_shape = domain_shape.as_list()
    range_shape[self.axis] = range_shape[self.axis] - 1
    range_shape = tf.TensorShape(range_shape)

    self._domain_shape_value = domain_shape
    self._range_shape_value = range_shape
    self._batch_shape_value = tf.TensorShape([])

    super().__init__(dtype,
                     is_non_singular=None,
                     is_self_adjoint=None,
                     is_positive_definite=None,
                     is_square=None,
                     name=name,
                     parameters=parameters)

  def _transform(self, x, adjoint=False):

    if adjoint:
      paddings1 = [[0, 0]] * x.shape.rank
      paddings2 = [[0, 0]] * x.shape.rank
      paddings1[self.axis] = [1, 0]
      paddings2[self.axis] = [0, 1]
      x1 = tf.pad(x, paddings1) # pylint: disable=no-value-for-parameter
      x2 = tf.pad(x, paddings2) # pylint: disable=no-value-for-parameter
      x = x1 - x2
    else:
      slice1 = [slice(None)] * x.shape.rank
      slice2 = [slice(None)] * x.shape.rank
      slice1[self.axis] = slice(1, None)
      slice2[self.axis] = slice(None, -1)
      x1 = x[tuple(slice1)]
      x2 = x[tuple(slice2)]
      x = x1 - x2
    return x

  def _domain_shape(self):
    return self._domain_shape_value

  def _range_shape(self):
    return self._range_shape_value

  def _batch_shape(self):
    return self._batch_shape_value

  @property
  def axis(self):
    return self._axis


# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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

def conjugate_gradient(operator,
                       rhs,
                       preconditioner=None,
                       x=None,
                       tol=1e-5,
                       max_iter=20,
                       l2_regularizer=0.0,
                       name='conjugate_gradient'):
  r"""Conjugate gradient solver.

  Solves a linear system of equations `A*x = rhs` for self-adjoint, positive
  definite matrix `A` and right-hand side vector `rhs`, using an iterative,
  matrix-free algorithm where the action of the matrix A is represented by
  `operator`. The iteration terminates when either the number of iterations
  exceeds `max_iter` or when the residual norm has been reduced to `tol`
  times its initial value, i.e. \\(||rhs - A x_k|| <= tol ||rhs||\\).

  .. note::
    This function is mostly equivalent to
    `tf.linalg.experimental.conjugate_gradient`, except it adds support for
    complex-valued linear systems.

  Args:
    operator: A `LinearOperator` that is self-adjoint and positive definite.
    rhs: A possibly batched vector of shape `[..., N]` containing the right-hand
      size vector.
    preconditioner: A `LinearOperator` that approximates the inverse of `A`.
      An efficient preconditioner could dramatically improve the rate of
      convergence. If `preconditioner` represents matrix `M`(`M` approximates
      `A^{-1}`), the algorithm uses `preconditioner.apply(x)` to estimate
      `A^{-1}x`. For this to be useful, the cost of applying `M` should be
      much lower than computing `A^{-1}` directly.
    x: A possibly batched vector of shape `[..., N]` containing the initial
      guess for the solution.
    tol: A float scalar convergence tolerance.
    max_iter: An integer giving the maximum number of iterations.
    name: A name scope for the operation.

  Returns:
    output: A namedtuple representing the final state with fields:
      - i: A scalar `int32` `Tensor`. Number of iterations executed.
      - x: A rank-1 `Tensor` of shape `[..., N]` containing the computed
          solution.
      - r: A rank-1 `Tensor` of shape `[.., M]` containing the residual vector.
      - p: A rank-1 `Tensor` of shape `[..., N]`. `A`-conjugate basis vector.
      - gamma: \\(r \dot M \dot r\\), equivalent to  \\(||r||_2^2\\) when
        `preconditioner=None`.

  Raises:
    ValueError: If `operator` is not self-adjoint and positive definite.
  """
  if not (operator.is_self_adjoint and operator.is_positive_definite):
    raise ValueError('Expected a self-adjoint, positive definite operator.')

  cg_state = collections.namedtuple('CGState', ['i', 'x', 'r', 'p', 'gamma'])

  def stopping_criterion(i, state):
    return tf.math.logical_and(
        i < max_iter,
        tf.math.reduce_any(
            tf.math.real(tf.norm(state.r, axis=-1)) > tf.math.real(tol)))

  def dot(x, y):
    return tf.squeeze(
        tf.linalg.matvec(
            x[..., tf.newaxis],
            y, adjoint_a=True), axis=-1)

  def cg_step(i, state):  # pylint: disable=missing-docstring
    z = tf.linalg.matvec(operator, state.p)
    if l2_regularizer != 0.0:
      z += tf.cast(l2_regularizer, rhs.dtype) * state.p
    alpha = state.gamma / dot(state.p, z)
    x = state.x + alpha[..., tf.newaxis] * state.p
    r = state.r - alpha[..., tf.newaxis] * z
    if preconditioner is None:
      q = r
    else:
      q = preconditioner.matvec(r)
    gamma = dot(r, q)
    beta = gamma / state.gamma
    p = q + beta[..., tf.newaxis] * state.p
    return i + 1, cg_state(i + 1, x, r, p, gamma)

  # We now broadcast initial shapes so that we have fixed shapes per iteration.

  with tf.name_scope(name):
    broadcast_shape = tf.broadcast_dynamic_shape(
        tf.shape(rhs)[:-1],
        operator.batch_shape_tensor())
    if preconditioner is not None:
      broadcast_shape = tf.broadcast_dynamic_shape(
          broadcast_shape,
          preconditioner.batch_shape_tensor()
      )
    broadcast_rhs_shape = tf.concat([broadcast_shape, [tf.shape(rhs)[-1]]], -1)
    r0 = tf.broadcast_to(rhs, broadcast_rhs_shape)
    tol *= tf.norm(r0, axis=-1)

    if x is None:
      x = tf.zeros(
          broadcast_rhs_shape, dtype=rhs.dtype.base_dtype)
    else:
      r0 = rhs - tf.linalg.matvec(operator, x)
    if preconditioner is None:
      p0 = r0
    else:
      p0 = tf.linalg.matvec(preconditioner, r0)
    gamma0 = dot(r0, p0)
    i = tf.constant(0, dtype=tf.int32)
    state = cg_state(i=i, x=x, r=r0, p=p0, gamma=gamma0)
    _, state = tf.while_loop(
        stopping_criterion, cg_step, [i, state])
    return cg_state(
        state.i,
        x=state.x,
        r=state.r,
        p=state.p,
        gamma=state.gamma)
