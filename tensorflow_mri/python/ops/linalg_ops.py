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

import collections

import tensorflow as tf
import tensorflow_nufft as tfft

from tensorflow_mri.python.ops import fft_ops
from tensorflow_mri.python.ops import math_ops
from tensorflow_mri.python.ops import traj_ops
from tensorflow_mri.python.util import check_util
from tensorflow_mri.python.util import deprecation
from tensorflow_mri.python.util import linalg_imaging
from tensorflow_mri.python.util import tensor_util

from tensorflow.python.ops.linalg import linear_operator


@linear_operator.make_composite_tensor
class LinearOperatorMRI(linalg_imaging.LinalgImagingMixin,
                        tf.linalg.LinearOperator):
  """The MR imaging operator.

  The MR imaging operator is a linear operator that maps a [batch of] images to
  a [batch of] potentially multicoil spatial frequency (*k*-space) data.

  This object may represent an undersampled MRI operator and supports
  Cartesian and non-Cartesian *k*-space sampling. The user may provide a
  sampling `mask` to represent an undersampled Cartesian operator, or a
  `trajectory` to represent a non-Cartesian operator.

  This object may represent a multicoil MRI operator by providing coil
  `sensitivities`. Note that `mask`, `trajectory` and `density` should never
  have a coil dimension, including in the case of multicoil imaging. The coil
  dimension will be handled automatically.

  Args:
    image_shape: A `TensorShape` or a list of `ints` of length 2 or 3.
      The shape of the images that this operator acts on.
    mask: An optional `Tensor` of type `bool`. The sampling mask. Must have
      shape `[..., *S]`, where `S` is the `image_shape` and `...` is
      the batch shape, which can have any number of dimensions. If `mask` is
      passed, this operator represents an undersampled MRI operator.
    trajectory: An optional `Tensor` of type `float32` or `float64`. Must have
      shape `[..., M, N]`, where `N` is the rank (number of spatial dimensions),
      `M` is the number of samples in the encoded space and `...` is the batch
      shape, which can have any number of dimensions. If `trajectory` is passed,
      this operator represents a non-Cartesian MRI operator.
    density: An optional `Tensor` of type `float32` or `float64`. The sampling
      densities. Must have shape `[..., M]`, where `M` is the number of
      samples and `...` is the batch shape, which can have any number of
      dimensions. This input is only relevant for non-Cartesian MRI operators.
      If passed, the non-Cartesian operator will include sampling density
      compensation. Can also be set to `'auto'` to automatically estimate
      the sampling density from the `trajectory`. If `None`, the operator will
      not perform sampling density compensation.
    sensitivities: An optional `Tensor` of type `complex64` or `complex128`.
      The coil sensitivity maps. Must have shape `[..., C, *S]`, where `S`
      is the `image_shape`, `C` is the number of coils and `...` is the batch
      shape, which can have any number of dimensions.
    phase: An optional `Tensor` of type `float32` or `float64`.
    fft_norm: FFT normalization mode. Must be `None` (no normalization)
      or `'ortho'`. Defaults to `'ortho'`.
    sens_norm: A `bool`. Whether to normalize coil sensitivities. Defaults to
      `False`.
    dtype: The dtype of this operator. Must be `complex64` or `complex128`.
      Defaults to `complex64`.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               image_shape,
               mask=None,
               trajectory=None,
               density=None,
               sensitivities=None,
               phase=None,
               fft_norm='ortho',
               sens_norm=False,
               dtype=tf.complex64,
               name='LinearOperatorMRI'):
    parameters = dict(
        image_shape=image_shape,
        mask=mask,
        trajectory=trajectory,
        density=density,
        sensitivities=sensitivities,
        phase=phase,
        fft_norm=fft_norm,
        sens_norm=sens_norm,
        dtype=dtype,
        name=name)

    # Set dtype.
    dtype = tf.as_dtype(dtype)
    if dtype not in (tf.complex64, tf.complex128):
      raise ValueError(
          f"`dtype` must be `complex64` or `complex128`, but got: {str(dtype)}") 

    # Set image shape and rank.
    self._image_shape = tf.TensorShape(image_shape)
    if self._image_shape.rank not in (2, 3):
      raise ValueError(
          f"`image_shape` must have rank 2 or 3, but got: {image_shape}")
    if not self._image_shape.is_fully_defined():
      raise ValueError(
          f"`image_shape` must be fully defined, but got {image_shape}")
    self._rank = self._image_shape.rank
    self._image_axes = list(range(-self._rank, 0))
    
    # Assume scalar batch shape, then update according to inputs.
    batch_shape = tf.TensorShape([])
    batch_shape_tensor = tf.constant([], dtype=tf.int32)

    # Set sampling mask after checking dtype and static shape.
    if mask is not None:
      mask = tf.convert_to_tensor(mask)
      if mask.dtype != tf.bool:
        raise TypeError(
            f"`mask` must have dtype `bool`, but got: {str(mask.dtype)}")
      if not mask.shape[-self._rank:].is_compatible_with(self._image_shape):
        raise ValueError(
            f"Expected the last dimensions of `mask` to be compatible with "
            f"{self._image_shape}], but got: {mask.shape[-self._rank:]}")
      batch_shape = tf.broadcast_static_shape(
          batch_shape, mask.shape[:-self._rank])
      batch_shape_tensor = tf.broadcast_dynamic_shape(
          batch_shape_tensor, tf.shape(mask)[:-self._rank])
    self._mask = mask

    # Set sampling trajectory after checking dtype and static shape.
    if trajectory is not None:
      if mask is not None:
        raise ValueError("`mask` and `trajectory` cannot be both passed.")
      trajectory = tf.convert_to_tensor(trajectory)
      if trajectory.dtype != dtype.real_dtype:
        raise TypeError(
            f"Expected `trajectory` to have dtype `{str(dtype.real_dtype)}`, "
            f"but got: {str(trajectory.dtype)}")
      if trajectory.shape[-1] != self._rank:
        raise ValueError(
            f"Expected the last dimension of `trajectory` to be {self._rank}, "
            f"but got {trajectory.shape[-1]}")
      batch_shape = tf.broadcast_static_shape(
          batch_shape, trajectory.shape[:-2])
      batch_shape_tensor = tf.broadcast_dynamic_shape(
          batch_shape_tensor, tf.shape(trajectory)[:-2])
    self._trajectory = trajectory

    # Set sampling density after checking dtype and static shape. If `'auto'`,
    # then estimate the density from the trajectory.
    if density is not None:
      if density is 'auto':  # Automatic density estimation.
        if self._trajectory is None:  # Cartesian operator: no density comp.
          density = None
        else:  # Non-Cartesian operator: estimate density.
          density = traj_ops.estimate_density(
              self._trajectory, self._image_shape)
      else:
        if self._trajectory is None:
          raise ValueError("`density` must be passed with `trajectory`.")
        density = tf.convert_to_tensor(density)
        if density.dtype != dtype.real_dtype:
          raise TypeError(
              f"Expected `density` to have dtype `{str(dtype.real_dtype)}`, "
              f"but got: {str(density.dtype)}")
        if density.shape[-1] != self._trajectory.shape[-2]:
          raise ValueError(
              f"Expected the last dimension of `density` to be "
              f"{self._trajectory.shape[-2]}, but got {density.shape[-1]}")
        batch_shape = tf.broadcast_static_shape(
            batch_shape, density.shape[:-1])
        batch_shape_tensor = tf.broadcast_dynamic_shape(
          batch_shape_tensor, tf.shape(density)[:-1])
    self._density = density

    # Set sensitivity maps after checking dtype and static shape.
    if sensitivities is not None:
      sensitivities = tf.convert_to_tensor(sensitivities)
      if sensitivities.dtype != dtype:
        raise TypeError(
            f"Expected `sensitivities` to have dtype `{str(dtype)}`, but got: "
            f"{str(sensitivities.dtype)}")
      if not sensitivities.shape[-self._rank:].is_compatible_with(
          self._image_shape):
        raise ValueError(
            f"Expected the last dimensions of `sensitivities` to be "
            f"compatible with {self._image_shape}, but got: "
            f"{sensitivities.shape[-self._rank:]}")
      batch_shape = tf.broadcast_static_shape(
          batch_shape, sensitivities.shape[:-(self._rank + 1)])
      batch_shape_tensor = tf.broadcast_dynamic_shape(
          batch_shape_tensor, tf.shape(sensitivities)[:-(self._rank + 1)])
    self._sensitivities = sensitivities

    if phase is not None:
      phase = tf.convert_to_tensor(phase)
      if phase.dtype != dtype.real_dtype:
        raise TypeError(
            f"Expected `phase` to have dtype `{str(dtype.real_dtype)}`, "
            f"but got: {str(phase.dtype)}")
      if not phase.shape[-self._rank:].is_compatible_with(self._image_shape):
        raise ValueError(
            f"Expected the last dimensions of `phase` to be "
            f"compatible with {self._image_shape}, but got: "
            f"{phase.shape[-self._rank:]}")
      batch_shape = tf.broadcast_static_shape(
          batch_shape, phase.shape[:-self._rank])
      batch_shape_tensor = tf.broadcast_dynamic_shape(
          batch_shape_tensor, tf.shape(phase)[:-self._rank])
    self._phase = phase

    # Set batch shapes.
    self._batch_shape_value = batch_shape
    self._batch_shape_tensor_value = batch_shape_tensor

    # If multicoil, add coil dimension to mask, trajectory and density.
    if self._sensitivities is not None:
      if self._mask is not None:
        self._mask = tf.expand_dims(self._mask, axis=-(self._rank + 1))
      if self._trajectory is not None:
        self._trajectory = tf.expand_dims(self._trajectory, axis=-3)
      if self._density is not None:
        self._density = tf.expand_dims(self._density, axis=-2)
      if self._phase is not None:
        self._phase = tf.expand_dims(self._phase, axis=-(self._rank + 1))

    # Save some tensors for later use during computation.
    if self._mask is not None:
      self._mask_linop_dtype = tf.cast(self._mask, dtype)
    if self._density is not None:
      self._dens_weights_sqrt = tf.cast(
          tf.math.sqrt(tf.math.reciprocal_no_nan(self._density)), dtype)
    if self._phase is not None:
      self._phase_rotator = tf.math.exp(
          tf.complex(tf.constant(0.0, dtype=phase.dtype), phase))

    # Set normalization.
    self._fft_norm = check_util.validate_enum(
        fft_norm, {None, 'ortho'}, 'fft_norm')
    if self._fft_norm == 'ortho':  # Compute normalization factors.
      self._fft_norm_factor = tf.math.reciprocal(
          tf.math.sqrt(tf.cast(self._image_shape.num_elements(), dtype)))

    # Normalize coil sensitivities.
    self._sens_norm = sens_norm
    if self._sens_norm:
      self._sensitivities = math_ops.normalize_no_nan(
          self._sensitivities, axis=-(self._rank + 1))

    super().__init__(dtype, name=name, parameters=parameters)

  def _transform(self, x, adjoint=False):
    """Transform [batch] input `x`."""
    if adjoint:
      # Apply density compensation.
      if self._density is not None:
        x *= self._dens_weights_sqrt

      # Apply adjoint Fourier operator.
      if self.is_non_cartesian:  # Non-Cartesian imaging, use NUFFT.
        x = tfft.nufft(x, self._trajectory,
                       grid_shape=self._image_shape,
                       transform_type='type_1',
                       fft_direction='backward')
        if self._fft_norm is not None:
          x *= self._fft_norm_factor

      else:  # Cartesian imaging, use FFT.
        if self._mask is not None:
          x *= self._mask_linop_dtype  # Undersampling.
        x = fft_ops.ifftn(x, axes=self._image_axes,
                          norm=self._fft_norm or 'forward', shift=True)

      # Apply coil combination.
      if self.is_multicoil:
        x *= tf.math.conj(self._sensitivities)
        x = tf.math.reduce_sum(x, axis=-(self._rank + 1))

      # Maybe remove phase from image.
      if self.is_phase_constrained:
        x *= tf.math.conj(self._phase_rotator)
        x = tf.cast(tf.math.real(x), self.dtype)

    else:  # Forward operator.

      # Add phase to real-valued image if reconstruction is phase-constrained.
      if self.is_phase_constrained:
        x = tf.cast(tf.math.real(x), self.dtype)
        x *= self._phase_rotator

      # Apply sensitivity modulation.
      if self.is_multicoil:
        x = tf.expand_dims(x, axis=-(self._rank + 1))
        x *= self._sensitivities

      # Apply Fourier operator.
      if self.is_non_cartesian:  # Non-Cartesian imaging, use NUFFT.
        x = tfft.nufft(x, self._trajectory,
                       transform_type='type_2',
                       fft_direction='forward')
        if self._fft_norm is not None:
          x *= self._fft_norm_factor

      else:  # Cartesian imaging, use FFT.
        x = fft_ops.fftn(x, axes=self._image_axes,
                         norm=self._fft_norm or 'backward', shift=True)
        if self._mask is not None:
          x *= self._mask_linop_dtype  # Undersampling.

      # Apply density compensation.
      if self._density is not None:
        x *= self._dens_weights_sqrt

    return x
  
  def _domain_shape(self):
    """Returns the shape of the domain space of this operator."""
    return self._image_shape

  def _range_shape(self):
    """Returns the shape of the range space of this operator."""
    if self.is_cartesian:
      range_shape = self._image_shape.as_list()
    else:
      range_shape = [self._trajectory.shape[-2]]
    if self.is_multicoil:
      range_shape = [self.num_coils] + range_shape
    return tf.TensorShape(range_shape)

  def _batch_shape(self):
    """Returns the static batch shape of this operator."""
    return self._batch_shape_value
  
  def _batch_shape_tensor(self):
    """Returns the dynamic batch shape of this operator."""
    return self._batch_shape_tensor_value

  @property
  def image_shape(self):
    """The image shape."""
    return self._image_shape

  @property
  def rank(self):
    """The number of spatial dimensions."""
    return self._rank

  @property
  def is_cartesian(self):
    """Whether this is a Cartesian MRI operator."""
    return self._trajectory is None

  @property
  def is_non_cartesian(self):
    """Whether this is a non-Cartesian MRI operator."""
    return self._trajectory is not None

  @property
  def is_multicoil(self):
    """Whether this is a multicoil MRI operator."""
    return self._sensitivities is not None

  @property
  def is_phase_constrained(self):
    """Whether this is a phase-constrained MRI operator."""
    return self._phase is not None

  @property
  def num_coils(self):
    """The number of coils."""
    if self._sensitivities is None:
      return None
    return self._sensitivities.shape[-(self._rank + 1)]

  @property
  def _composite_tensor_fields(self):
    return ("image_shape", "mask", "trajectory", "density", "sensitivities",
            "fft_norm")


@linear_operator.make_composite_tensor
class LinearOperatorGramMatrix(linalg_imaging.LinalgImagingMixin,
                               tf.linalg.LinearOperator):
  """Gram matrix of a linear operator.

  If :math:`A` is a `LinearOperator`, this operator is equivalent to
  :math:`A^H A`.

  The Gram matrix of :math:`A` appears in the normal equation
  :math:`A^H A x = A^H b` associated with the least squares problem 
  :math:`\min_x{\frac{1}{2} \left \| Ax-b \right \|_2^2}`.

  This operator is self-adjoint and positive definite. Therefore, linear systems
  defined by this linear operator can be solved using the conjugate gradient
  method.
  """
  def __init__(self,
               operator,
               reg_parameter=None,
               name=None):
    parameters = dict(
        operator=operator,
        reg_parameter=reg_parameter,
        name=name)
    self._operator = operator
    self._reg_parameter = reg_parameter
    self._reg_operator = None
    self._composed = linalg_imaging.LinearOperatorComposition(
        operators=[self._operator.H, self._operator])

    if self._reg_parameter is not None:
      if self._reg_operator is None:
        self._reg_operator = linalg_imaging.LinearOperatorScaledIdentity(
            shape=self._operator.domain_shape,
            multiplier=tf.cast(self._reg_parameter, self._operator.dtype))
      self._composed = linalg_imaging.LinearOperatorAddition(
          operators=[self._composed, self._reg_operator])

    super().__init__(operator.dtype,
                     is_self_adjoint=True,
                     is_positive_definite=True,
                     is_square=True,
                     parameters=parameters)

  def _transform(self, x, adjoint=False):
    return self._composed.transform(x, adjoint=adjoint)

  def _domain_shape(self):
    return self.operator.domain_shape

  def _range_shape(self):
    return self.operator.domain_shape
  
  def _batch_shape(self):
    return self.operator.batch_shape

  def _domain_shape_tensor(self):
    return self.operator.domain_shape_tensor()
  
  def _range_shape_tensor(self):
    return self.operator.domain_shape_tensor()

  def _batch_shape_tensor(self):
    return self.operator.batch_shape_tensor()

  @property
  def operator(self):
    return self._operator


class LinearOperatorFFT(linalg_imaging.LinalgImagingMixin): # pylint: disable=abstract-method
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


class LinearOperatorNUFFT(linalg_imaging.LinalgImagingMixin): # pylint: disable=abstract-method
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


class LinearOperatorSensitivityModulation(linalg_imaging.LinalgImagingMixin): # pylint: disable=abstract-method
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
    norm: A `bool`. If `True`, the coil sensitivites are normalized to have
      unit L2 norm along the channel dimension. Defaults to `False`.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               sensitivities,
               rank=None,
               norm=False,
               name='LinearOperatorSensitivityModulation'):

    parameters = dict(
      sensitivities=sensitivities,
      rank=rank,
      norm=norm,
      name=name
    )

    self._sensitivities = check_util.validate_tensor_dtype(
        tf.convert_to_tensor(sensitivities), 'complex', name='sensitivities')

    self._rank = rank or self.sensitivities.shape.rank - 1
    self._image_shape = self.sensitivities.shape[-self.rank:]
    self._coil_axis = -(self.rank + 1)
    self._num_coils = self.sensitivities.shape[self._coil_axis]

    self._domain_shape_value = self.image_shape
    self._range_shape_value = tf.TensorShape([self.num_coils]) + self.image_shape
    self._batch_shape_value = self.sensitivities.shape[:self._coil_axis]

    self._norm = norm
    if self._norm:
      self._sensitivities = tf.math.divide_no_nan(
          self._sensitivities,
          tf.norm(self._sensitivities, axis=self._coil_axis, keepdims=True))

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
      x = tf.expand_dims(x, axis=self._coil_axis)
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


@deprecation.deprecated(None, "Use `LinearOperatorMRI` instead.")
class LinearOperatorParallelMRI(linalg_imaging.LinearOperatorComposition): # pylint: disable=abstract-method
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
    fft_normalization: FFT normalization mode. Must be `None` (no normalization)
      or `'ortho'`. Defaults to `'ortho'`.
    normalize_sensitivities: A `bool`. If `True`, the coil sensitivity maps
      are normalized to have unit L2 norm along the coil dimension. Defaults to
      `False`.
    name: An optional `string`. The name of this operator.
  """
  def __init__(self,
               sensitivities,
               mask=None,
               trajectory=None,
               rank=None,
               fft_normalization='ortho',
               normalize_sensitivities=False,
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
          sensitivities.shape[-self.rank-1:], trajectory, # pylint: disable=invalid-unary-operand-type
          norm=fft_normalization)
    else: # Cartesian      
      linop_fourier = LinearOperatorFFT(
          sensitivities.shape[-self.rank-1:], rank=rank, mask=mask, # pylint: disable=abstract-class-instantiated,invalid-unary-operand-type
          norm=fft_normalization)

    # Prepare the coil sensitivity operator.
    linop_sens = LinearOperatorSensitivityModulation(
        sensitivities, rank=self.rank, norm=normalize_sensitivities)

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


class LinearOperatorRealWeighting(linalg_imaging.LinalgImagingMixin): # pylint: disable=abstract-method
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


class LinearOperatorImagingDifference(linalg_imaging.LinalgImagingMixin,
                                      tf.linalg.LinearOperator):
  """Linear operator acting like a difference operator.

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
               name="LinearOperatorImagingDifference"):

    parameters = dict(
      domain_shape=domain_shape,
      axis=axis,
      dtype=dtype,
      name=name
    )

    domain_shape = tf.TensorShape(domain_shape)

    self._axis = check_util.validate_axis(axis, domain_shape.rank,
                                          max_length=1,
                                          canonicalize="negative",
                                          scalar_to_list=False)

    range_shape = domain_shape.as_list()
    range_shape[self.axis] = range_shape[self.axis] - 1
    range_shape = tf.TensorShape(range_shape)

    self._domain_shape_value = domain_shape
    self._range_shape_value = range_shape

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
                       name=None):
  r"""Conjugate gradient solver.

  Solves a linear system of equations `A*x = rhs` for self-adjoint, positive
  definite matrix `A` and right-hand side vector `rhs`, using an iterative,
  matrix-free algorithm where the action of the matrix A is represented by
  `operator`. The iteration terminates when either the number of iterations
  exceeds `max_iter` or when the residual norm has been reduced to `tol`
  times its initial value, i.e. \\(||rhs - A x_k|| <= tol ||rhs||\\).

  .. note::
    This function is similar to
    `tf.linalg.experimental.conjugate_gradient`, except it adds support for
    complex-valued linear systems and for imaging operators.

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
    A namedtuple representing the final state with fields:
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
  if isinstance(operator, linalg_imaging.LinalgImagingMixin):
    rhs = operator.flatten_domain_shape(rhs)

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

  with tf.name_scope(name or 'conjugate_gradient'):
    broadcast_shape = tf.broadcast_dynamic_shape(
        tf.shape(rhs)[:-1],
        operator.batch_shape_tensor())
    static_broadcast_shape = tf.broadcast_static_shape(
        rhs.shape[:-1],
        operator.batch_shape)
    if preconditioner is not None:
      broadcast_shape = tf.broadcast_dynamic_shape(
          broadcast_shape,
          preconditioner.batch_shape_tensor())
      static_broadcast_shape = tf.broadcast_static_shape(
          static_broadcast_shape,
          preconditioner.batch_shape)
    broadcast_rhs_shape = tf.concat([broadcast_shape, [tf.shape(rhs)[-1]]], -1)
    static_broadcast_rhs_shape = static_broadcast_shape.concatenate(
        [rhs.shape[-1]])
    r0 = tf.broadcast_to(rhs, broadcast_rhs_shape)
    tol *= tf.norm(r0, axis=-1)

    if x is None:
      x = tf.zeros(
          broadcast_rhs_shape, dtype=rhs.dtype.base_dtype)
      x = tf.ensure_shape(x, static_broadcast_rhs_shape)
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

    if isinstance(operator, linalg_imaging.LinalgImagingMixin):
      x = operator.expand_range_dimension(state.x)
    else:
      x = state.x

    return cg_state(
        state.i,
        x=x,
        r=state.r,
        p=state.p,
        gamma=state.gamma)
