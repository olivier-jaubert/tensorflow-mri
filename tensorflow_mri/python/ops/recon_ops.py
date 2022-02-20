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
"""Operators for MR image reconstruction.

Image reconstruction operators accept *k*-space data and additional
application-dependent inputs and return an image.

This module contains 4 operators for image reconstruction, all of which support
Cartesian and non-Cartesian inputs.

  * `tfmri.reconstruct_adj`: Reconstructs an image by applying the adjoint MRI
    operator to the *k*-space data. This typically involves an inverse FFT or
    a (density-compensated) NUFFT, and coil combination for multicoil inputs.
    This type of reconstruction is often called zero-filled reconstruction,
    because missing *k*-space samples are assumed to be zero. Therefore, the
    resulting image is likely to display aliasing artefacts if *k*-space is not
    sufficiently sampled according to the Nyquist criterion.

  * `tfmri.reconstruct_lstsq`: Reconstructs an image by formulating a (possibly
    regularized) least squares problem, which is solved iteratively. Since the
    problem may be ill-posed, different types of regularizers may be used to
    incorporate prior knowledge. Depending on the regularizer, the optimization
    problem may be linear or nonlinear. For sparsity-based regularizers, this
    is also called a compressed sensing reconstruction. This is a powerful
    operator which can often produce high-quality images even from highly
    undersampled *k*-space data. However, it may be time-consuming, depending
    on the characteristics of the problem.
  
  * `tfmri.reconstruct_ai`: Reconstructs an image using an AI model.

  * `tfmri.reconstruct_legacy`: Reconstructs an image using legacy methods such
    as SENSE or GRAPPA.
"""

import collections

import tensorflow as tf
import tensorflow_nufft as tfft

from tensorflow_mri.python.ops import array_ops
from tensorflow_mri.python.ops import coil_ops
from tensorflow_mri.python.ops import convex_ops
from tensorflow_mri.python.ops import fft_ops
from tensorflow_mri.python.ops import image_ops
from tensorflow_mri.python.ops import linalg_ops
from tensorflow_mri.python.ops import math_ops
from tensorflow_mri.python.ops import optimizer_ops
from tensorflow_mri.python.ops import signal_ops
from tensorflow_mri.python.ops import traj_ops
from tensorflow_mri.python.util import check_util
from tensorflow_mri.python.util import tensor_util


def reconstruct(kspace,
                mask=None,
                trajectory=None,
                density=None,
                calib=None,
                sensitivities=None,
                method=None,
                **kwargs):
  """MR image reconstruction gateway.

  Reconstructs an image given the corresponding *k*-space measurements.

  This is a gateway function to different image reconstruction methods. The
  reconstruction method can be selected with the `method` argument. If the
  `method` argument is not specified, a method is automatically selected based
  on the input arguments.

  Supported methods are:

  * **fft**: Simple fast Fourier transform (FFT) reconstruction for Cartesian
    *k*-space data. This is the default method if only a `kspace` argument is
    given.
  * **nufft**: Non-uniform fast Fourier transform (NUFFT) reconstruction for
    non-Cartesian *k*-space data. Uses the adjoint NUFFT operator with density
    compensation. This is the default method if `kspace`, `trajectory` and
    (optionally) `density` are given.
  * **inufft**: Non-uniform fast Fourier transform (NUFFT) reconstruction for
    non-Cartesian *k*-space data. Uses the inverse NUFFT, calculated
    iteratively. This method is never selected by default.
  * **sense**: SENSitivity Encoding (SENSE) [1]_ reconstruction for Cartesian
    *k*-space data. This is the default method if `kspace` and `sensitivities`
    are given.
  * **cg_sense**: Conjugate gradient SENSE (CG-SENSE) [2]_ reconstruction for
    non-Cartesian *k*-space data. This is the default method if `kspace`,
    `trajectory`, `sensitivities` and (optionally) `density` are given.
  * **grappa**: Generalized autocalibrating partially parallel acquisitions [3]_
    reconstruction for Cartesian *k*-space data. This is the default method if
    `kspace`, `calib` and (optionally) `sensitivities` are given.
  * **pics**: Combined parallel imaging and compressed sensing (PICS)
    reconstruction. Accepts Cartesian and non-Cartesian *k*-space data. Supply
    `mask` and `sensitivities` in combination with a Cartesian `kspace`, or
    `trajectory`, `sensitivities` and (optionally) `density` in combination with
    a non-Cartesian `kspace`. If `sensitivities` is not provided, a compressed
    sensing reconstruction is performed. This method is never selected by
    default.

  .. note::
    This function supports CPU and GPU computation.

  .. note::
    This function supports batches of inputs, which are processed in parallel
    whenever possible.

  See also `tfmri.estimate_coil_sensitivities` and `tfmri.combine_coils`.

  Args:
    kspace: A `Tensor`. The *k*-space samples. Must have type `complex64` or
      `complex128`. `kspace` can be either Cartesian or non-Cartesian. A
      Cartesian `kspace` must have shape `[..., C, *K]`, where `K` is the shape
      of the spatial frequency dimensions, `C` is the number of coils and `...`
      is the batch shape, which can have any rank. Note that `K` should be the
      reduced or undersampled shape, i.e., no zero-filling of any kind should be
      included. A non-Cartesian `kspace` must have shape `[..., C, M]`, where
      `M` is the number of samples, `C` is the number of coils and `...` is the
      batch shape, which can have any rank.
    mask: A `Tensor`. The sampling mask. Must have type `bool`. Must have shape
      `S`, where `S` is the shape of the spatial dimensions. In other words,
      `mask` should have the shape of a fully sampled *k*-space. For each point,
      `mask` should be `True` if the corresponding *k*-space sample was measured
      and `False` otherwise. `True` entries should correspond to the data in
      `kspace`, and the result of dropping all `False` entries from `mask`
      should have shape `K`. `mask` is required if `method` is `"grappa"`, or
      if `method` is `"pics"` and `kspace` is Cartesian. For other methods or
      for non-Cartesian `kspace`, this parameter is not relevant.
    trajectory: A `Tensor`. The *k*-space trajectory. Must have type `float32`
      or `float64`. Must have shape `[..., M, N]`, where `N` is the number of
      spatial dimensions, `N` is the number of *k*-space samples and `...` is
      the batch shape, which can have any rank and must be broadcastable to the
      batch shape of `kspace`. `trajectory` is required when `method` is
      `"nufft"`, `"inufft"` or `"cg_sense"`, or if `method` is `"pics"` and
      `kspace` is non-Cartesian. For other methods or for Cartesian `kspace`,
      this parameter is not relevant.
    density: A `Tensor`. The sampling density. Must have type `float32` or
      `float64`. Must have shape `[..., M]`, where `M` is the number of
      *k*-space samples and `...` is the batch shape, which can have any rank
      and must be broadcastable to the batch shape of `kspace`. `density` is
      optional when `method` is `"nufft"` or `"cg_sense"`, or if `method` is
      `"pics"` and `kspace` is non-Cartesian. In these cases, `density` will be
      estimated from the given `trajectory` if not provided. For other methods
      or for Cartesian `kspace`, this parameter is not relevant.
    calib: A `Tensor`. The calibration data. Must have type `complex64` or
      `complex128`. Must have shape `[..., C, *R]`, where `R` is the shape of
      the calibration region, `C` is the number of coils and `...` is the batch
      shape, which can have any rank and must be broadcastable to the batch
      shape of `kspace`. `calib` is required when `method` is `"grappa"`. For
      other methods, this parameter is not relevant.
    sensitivities: A `Tensor`. The coil sensitivity maps. Must have type
      `complex64` or `complex128`. Must have shape `[..., C, *S]`, where `S` is
      shape of the spatial dimensions, `C` is the number of coils and `...` is
      the batch shape, which can have any rank and must be broadcastable to the
      batch shape of `kspace`. `sensitivities` is required when `method` is
      `"sense"` or `"cg_sense"`. For other methods, this parameter is not
      relevant.
    method: A `string`. The reconstruction method. Must be one of `"fft"`,
      `"nufft"`, `"inufft"`, `"sense"`, `"cg_sense"`, `"grappa"` or `"pics"`.
    **kwargs: Additional method-specific keyword arguments. See Notes for the
      method-specific arguments.

  Notes:
    This function accepts several method dependent arguments:

    * For `method="fft"`, provide `kspace` and, optionally, `sensitivities`.
      If provided, `sensitivities` are used for adaptive coil combination (see
      `tfmri.combine_coils`). If not provided, multi-coil inputs are combined
      using the sum of squares method. In addition, the following keyword
      arguments are accepted:

      * **rank**: An optional `int`. The rank (in the sense of spatial
        dimensionality) of this operation. Defaults to `kspace.shape.rank` if
        `multicoil` is `False` and `kspace.shape.rank - 1` if `multicoil` is
        `True`.
      * **multicoil**: An optional `bool`. Whether the input *k*-space has a
        coil dimension. Defaults to `True` if `sensitivities` were specified,
        `False` otherwise.
      * **combine_coils**: An optional `bool`. If `True`, multi-coil images
        are combined. Otherwise, the uncombined images are returned. Defaults to
        `True`.

    * For `method="nufft"`, provide `kspace`, `trajectory` and, optionally,
      `density` and `sensitivities`. If `density` is not provided, an estimate
      will be used (see `tfmri.estimate_density`). If provided, `sensitivities`
      are used for adaptive coil combination (see `tfmri.combine_coils`). If not
      provided, multi-coil inputs are combined using the sum of squares method.
      In addition, the following keyword arguments are accepted:

      * **image_shape**: A `TensorShape` or list of `ints`. The shape of the
        output images. This parameter must be provided.
      * **multicoil**: An optional `bool`. Whether the input *k*-space has a
        coil dimension. Defaults to `True` if `sensitivities` were specified,
        `False` otherwise.
      * **combine_coils**: An optional `bool`. If `True`, multi-coil images
        are combined. Otherwise, the uncombined images are returned. Defaults to
        `True`.

    * For `method="inufft"`, provide `kspace`, `trajectory` and, optionally,
      `sensitivities`. If provided, `sensitivities` are used for adaptive coil
      combination (see `tfmri.combine_coils`). If not provided, multi-coil inputs
      are combined using the sum of squares method. In addition, the following
      arguments are accepted:

      * **image_shape**: A `TensorShape` or list of `ints`. The shape of the
        output images. This parameter must be provided.
      * **tol**: An optional `float`. The convergence tolerance for the
        conjugate gradient iteration. Defaults to 1e-05.
      * **max_iter**: An optional `int`. The maximum number of iterations for
        the conjugate gradient iteration. Defaults to 10.
      * **return_cg_state**: An optional `bool`. Defaults to `False`. If `True`,
        return a tuple containing the image and an object describing the final
        state of the CG iteration. For more details about the CG state, see
        `tfmri.conjugate_gradient`. If `False`, only the image is returned.
      * **multicoil**: An optional `bool`. Whether the input *k*-space has a
        coil dimension. Defaults to `True` if `sensitivities` were specified,
        `False` otherwise.
      * **combine_coils**: An optional `bool`. If `True`, multi-coil images
        are combined. Otherwise, the uncombined images are returned. Defaults to
        `True`.

    * For `method="sense"`, provide `kspace` and `sensitivities`. In addition,
      the following keyword arguments are accepted:

      * **reduction_axis**: An `int` or a list of `ints`. The reduced axes. This
        parameter must be provided.
      * **reduction_factor**: An `int` or a list of `ints`. The reduction
        factors corresponding to each reduction axis. The output image will have
        dimension `kspace.shape[ax] * r` for each pair `ax` and `r` in
        `reduction_axis` and `reduction_factor`. This parameter must be
        provided.
      * **rank**: An optional `int`. The rank (in the sense of spatial
        dimensionality) of this operation. Defaults to `kspace.shape.rank - 1`.
        Therefore, if `rank` is not specified, axis 0 is interpreted to be the
        coil axis and the remaining dimensions are interpreted to be spatial
        dimensions. You must specify `rank` if you intend to provide any batch
        dimensions in `kspace` and/or `sensitivities`.
      * **l2_regularizer**: An optional `float`. The L2 regularization factor
        used when solving the linear least-squares problem. Ignored if
        `fast=False`. Defaults to 0.0.
      * **fast**: An optional `bool`. Defaults to `True`. If `False`, use a
        numerically robust orthogonal decomposition method to solve the linear
        least-squares. This algorithm finds the solution even for rank deficient
        matrices, but is significantly slower. For more details, see
        `tf.linalg.lstsq`.

    * For `method="cg_sense"`, provide `kspace`, `trajectory`, `density`
      (optional) and `sensitivities`. If `density` is not provided, an estimate
      will be used (see `tfmri.estimate_density`). In addition, the following
      keyword arguments are accepted:

      * **tol**: An optional `float`. The convergence tolerance for the
        conjugate gradient iteration. Defaults to 1e-05.
      * **max_iter**: An optional `int`. The maximum number of iterations for
        the conjugate gradient iteration. Defaults to 10.
      * **return_cg_state**: An optional `bool`. Defaults to `False`. If `True`,
        return a tuple containing the image and an object describing the final
        state of the CG iteration. For more details about the CG state, see
        `tfmri.conjugate_gradient`. If `False`, only the image is returned.

    * For `method="grappa"`, provide `kspace`, `mask` and `calib`. Optionally,
      you can also provide `sensitivities` (note that `sensitivities` are not
      used for the GRAPPA computation, but they are used for adaptive coil
      combination). If `sensitivities` are not provided, coil combination will
      be performed using the sum of squares method. Additionally, the following
      keyword arguments are accepted:

      * **kernel_size**: An `int` or list of `ints`. The size of the GRAPPA
        kernel. Must have length equal to the image rank or number of spatial
        dimensions. If a scalar `int` is provided, the same size is used in all
        dimensions.
      * **weights_l2_regularizer**: An optional `float`. The regularization
        factor for the L2 regularization term used to fit the GRAPPA weights.
        If 0.0, no regularization is applied.
      * **combine_coils**: An optional `bool`. If `True`, multi-coil images
        are combined. Otherwise, the uncombined images are returned. Defaults to
        `True`.
      * **return_kspace**: An optional `bool`. If `True`, returns the filled
        *k*-space without performing the Fourier transform. In this case, coils
        are not combined regardless of the value of `combine_coils`.

    * For `method="pics"`, provide `kspace`, `mask` (Cartesian only),
      `trajectory` (non-Cartesian only), `density` (non-Cartesian only,
      optional) and `sensitivities` (optional). Additionally, the following
      keyword arguments are accepted:

      * **recon_shape**: A `tf.TensorShape` or a list of `int`. The shape of
        the reconstructed image, including temporal dimensions but not batch
        dimensions. This argument must be provided.
      * **rank**: An `int`. The number of spatial dimensions.
      * **regularizers**: A list of `tfmri.Regularizer`. The regularizers to be
        used in the iterative reconstruction.
      * **optimizer**: The optimizer. Must be `None` or `"lbfgs"`. If `None`,
        the optimizer will be selected automatically.
      * **initial_image**: A `Tensor`. The initial estimate for the iterative
        reconstruction. Must have shape `recon_shape`.
      * **max_iterations**: An `int`. The maximum number of iterations.
      * **use_density_compensation**: A `bool`. If `True`, adds an explicit
        density compensation step to the encoding operator.

  Returns:
    A `Tensor`. The reconstructed images. Has the same type as `kspace`. Has
    shape `[..., S]`, where `...` is the batch shape of `kspace` and `S` is the
    spatial shape.

  References:
    .. [1] Pruessmann, K.P., Weiger, M., Scheidegger, M.B. and Boesiger, P.
      (1999), SENSE: Sensitivity encoding for fast MRI. Magn. Reson. Med.,
      42: 952-962.
      https://doi.org/10.1002/(SICI)1522-2594(199911)42:5<952::AID-MRM16>3.0.CO;2-S

    .. [2] Pruessmann, K.P., Weiger, M., Börnert, P. and Boesiger, P. (2001),
      Advances in sensitivity encoding with arbitrary k-space trajectories.
      Magn. Reson. Med., 46: 638-651. https://doi.org/10.1002/mrm.1241

    .. [3] Griswold, M.A., Jakob, P.M., Heidemann, R.M., Nittka, M., Jellus, V.,
      Wang, J., Kiefer, B. and Haase, A. (2002), Generalized autocalibrating
      partially parallel acquisitions (GRAPPA). Magn. Reson. Med., 47:
      1202-1210. https://doi.org/10.1002/mrm.10171

    .. [4] Block, K.T., Uecker, M. and Frahm, J. (2007), Undersampled radial MRI
      with multiple coils. Iterative image reconstruction using a total
      variation constraint. Magn. Reson. Med., 57: 1086-1098.
      https://doi.org/10.1002/mrm.21236

    .. [5] Feng, L., Grimm, R., Block, K.T., Chandarana, H., Kim, S., Xu, J.,
      Axel, L., Sodickson, D.K. and Otazo, R. (2014), Golden-angle radial sparse
      parallel MRI: Combination of compressed sensing, parallel imaging, and
      golden-angle radial sampling for fast and flexible dynamic volumetric MRI.
      Magn. Reson. Med., 72: 707-717. https://doi.org/10.1002/mrm.24980
  """
  method = _select_reconstruction_method(
    kspace, mask, trajectory, density, calib, sensitivities, method)

  kspace = tf.convert_to_tensor(kspace)
  if mask is not None:
    mask = tf.convert_to_tensor(mask)
  if trajectory is not None:
    trajectory = tf.convert_to_tensor(trajectory)
  if density is not None:
    density = tf.convert_to_tensor(density)
  if sensitivities is not None:
    sensitivities = tf.convert_to_tensor(sensitivities)

  args = {'mask': mask,
          'trajectory': trajectory,
          'density': density,
          'calib': calib,
          'sensitivities': sensitivities}

  args = {name: arg for name, arg in args.items() if arg is not None}

  return _MR_RECON_METHODS[method](kspace, **{**args, **kwargs})


def _fft(kspace,
         sensitivities=None,
         rank=None,
         multicoil=None,
         combine_coils=True):
  """MR image reconstruction using FFT.

  For the parameters, see `tfmri.reconstruct`.
  """
  kspace = tf.convert_to_tensor(kspace)
  if sensitivities is not None:
    sensitivities = tf.convert_to_tensor(sensitivities)

  # Check inputs and set defaults.
  if multicoil is None:
    # `multicoil` defaults to True if sensitivities were passed; False
    # otherwise.
    multicoil = sensitivities is not None
  if rank is None:
    # If `rank` not specified, assume no leading batch dimensions, so all dims
    # are spatial dims (minus coil dimension if `multicoil` is true).
    rank = kspace.shape.rank
    if multicoil:
      rank -= 1 # Account for coil dimension.
    if rank > 3:
      raise ValueError(
        f"Can only reconstruct images up to rank 3, but `kspace` has "
        f"{rank} spatial dimensions. If `kspace` has any leading batch "
        f"dimensions, please set the argument `rank` explicitly.")
  else:
    rank = check_util.validate_type(rank, int, "rank")
    if rank > 3:
      raise ValueError(f"Argument `rank` must be <= 3, but got: {rank}")

  # Do FFT.
  axes = list(range(-rank, 0)) # pylint: disable=invalid-unary-operand-type
  image = fft_ops.ifftn(kspace, axes=axes, shift=True)

  # If multicoil, do coil combination. Will do adaptive combine if
  # `sensitivities` are given, otherwise sum of squares.
  if multicoil and combine_coils:
    image = coil_ops.combine_coils(image, maps=sensitivities, coil_axis=-rank-1) # pylint: disable=invalid-unary-operand-type
  return image


def _nufft(kspace,
           trajectory,
           density=None,
           sensitivities=None,
           image_shape=None,
           multicoil=None,
           combine_coils=True):
  """MR image reconstruction using density-compensated adjoint NUFFT.

  For the parameters, see `tfmri.reconstruct`.
  """
  kspace = tf.convert_to_tensor(kspace)
  trajectory = tf.convert_to_tensor(trajectory)
  if density is not None:
    density = tf.convert_to_tensor(density)
  if sensitivities is not None:
    sensitivities = tf.convert_to_tensor(sensitivities)

  # Add channel dimension to trajectory and density.
  trajectory = tf.expand_dims(trajectory, -3)
  if density is not None:
    density = tf.expand_dims(density, -2)

  # Infer rank from number of dimensions in trajectory.
  rank = trajectory.shape[-1]
  if rank > 3:
    raise ValueError(
        f"Can only reconstruct images up to rank 3, but `trajectory` implies "
        f"rank {rank}.")
  # Check inputs and set defaults.
  if image_shape is None:
    # `image_shape` is required.
    raise ValueError("Argument `image_shape` must be provided for NUFFT.")
  image_shape = tf.TensorShape(image_shape)
  image_shape.assert_has_rank(rank)

  if multicoil is None:
    # `multicoil` defaults to True if sensitivities were passed; False
    # otherwise.
    multicoil = sensitivities is not None

  # Compensate non-uniform sampling density.
  if density is None:
    density = traj_ops.estimate_density(trajectory, image_shape)
  kspace = tf.math.divide_no_nan(kspace, tensor_util.cast_to_complex(density))

  # Do NUFFT.
  image = tfft.nufft(kspace, trajectory,
                     grid_shape=image_shape,
                     transform_type='type_1',
                     fft_direction='backward')

  # Do coil combination.
  if multicoil and combine_coils:
    image = coil_ops.combine_coils(image, maps=sensitivities, coil_axis=-rank-1)

  return image


def _inufft(kspace,
            trajectory,
            sensitivities=None,
            image_shape=None,
            tol=1e-5,
            max_iter=10,
            return_cg_state=False,
            multicoil=None,
            combine_coils=True):
  """MR image reconstruction using iterative inverse NUFFT.

  For the parameters, see `tfmri.reconstruct`.
  """
  kspace = tf.convert_to_tensor(kspace)
  trajectory = tf.convert_to_tensor(trajectory)

  if sensitivities is not None:
    sensitivities = tf.convert_to_tensor(sensitivities)

  # Infer rank from number of dimensions in trajectory.
  rank = trajectory.shape[-1]
  if rank > 3:
    raise ValueError(
        f"Can only reconstruct images up to rank 3, but `trajectory` implies "
        f"rank {rank}.")
  # Check inputs and set defaults.
  if image_shape is None:
    # `image_shape` is required.
    raise ValueError("Argument `image_shape` must be provided for NUFFT.")
  image_shape = tf.TensorShape(image_shape)
  image_shape.assert_has_rank(rank)

  if multicoil is None:
    # `multicoil` defaults to True if sensitivities were passed; False
    # otherwise.
    multicoil = sensitivities is not None

  batch_shape = tf.shape(kspace)[:-1]

  # Set up system operator and right hand side.
  linop_nufft = linalg_ops.LinearOperatorNUFFT(image_shape, trajectory)
  operator = tf.linalg.LinearOperatorComposition(
      [linop_nufft.H, linop_nufft],
      is_self_adjoint=True, is_positive_definite=True)

  # Compute right hand side.
  rhs = tf.linalg.matvec(linop_nufft.H, kspace)

  # Solve linear system using conjugate gradient iteration.
  result = linalg_ops.conjugate_gradient(operator, rhs, x=None,
                                         tol=tol, max_iter=max_iter)

  # Restore image shape.
  image = tf.reshape(result.x, tf.concat([batch_shape, image_shape], 0))

  # Do coil combination.
  if multicoil and combine_coils:
    image = coil_ops.combine_coils(image, maps=sensitivities, coil_axis=-rank-1)

  return (image, result) if return_cg_state else image


def _sense(kspace,
           sensitivities,
           reduction_axis,
           reduction_factor,
           rank=None,
           l2_regularizer=0.0,
           fast=True):
  """MR image reconstruction using SENSitivity Encoding (SENSE).

  For the parameters, see `tfmri.reconstruct`.
  """
  # Parse inputs.
  kspace = tf.convert_to_tensor(kspace)
  sensitivities = tf.convert_to_tensor(sensitivities)

  # Rank or spatial dimensionality.
  rank = rank or kspace.shape.rank - 1

  reduced_shape = kspace.shape[-rank:]
  reduction_axis = check_util.validate_list(
    reduction_axis, element_type=int, name='reduction_axis')
  reduction_factor = check_util.validate_list(
    reduction_factor, element_type=int, length=len(reduction_axis),
    name='reduction_factor')
  reduction_axis = [ax + rank if ax < 0 else ax for ax in reduction_axis]
  canonical_reduction = [1] * rank
  for ax, r in zip(reduction_axis, reduction_factor):
    canonical_reduction[ax] = r
  image_shape = tf.TensorShape(
    [s * r for s, r in zip(reduced_shape.as_list(), canonical_reduction)])

  # Compute batch shapes. `batch_shape` is the output batch shape.
  kspace_rank = kspace.shape.rank
  kspace_batch_shape = kspace.shape[:-rank-1]
  sens_rank = sensitivities.shape.rank
  sens_batch_shape = sensitivities.shape[:-rank-1]
  batch_shape = tf.broadcast_static_shape(kspace_batch_shape, sens_batch_shape)
  # We do not broadcast the k-space, by design.
  if batch_shape != kspace_batch_shape:
    raise ValueError(
      f"`kspace` and `sensitivities` have incompatible batch shapes: "
      f"{kspace_batch_shape}, {sens_batch_shape}")

  # Rearrange dimensions. Put spatial dimensions first, then coil dimension,
  # then batch dimensions.
  kspace_perm = list(range(-rank, 0)) + [-rank-1]
  kspace_perm = [ax + kspace_rank for ax in kspace_perm]
  kspace_perm += list(range(0, kspace_rank - rank - 1))
  sens_perm = list(range(-rank, 0)) + [-rank-1]
  sens_perm = [ax + sens_rank for ax in sens_perm]
  sens_perm += list(range(0, sens_rank - rank - 1))
  kspace = tf.transpose(kspace, kspace_perm)
  sensitivities = tf.transpose(sensitivities, sens_perm)

  # Compute aliased images and shift along the reduced dimensions.
  aliased_images = fft_ops.ifftn(kspace, axes=list(range(rank)), shift=True)
  aliased_images = tf.signal.ifftshift(aliased_images, axes=reduction_axis)

  # Create a grid of indices into the reduced FOV image.
  reduced_indices = tf.stack(tf.meshgrid(*[tf.range(s) for s in reduced_shape]))
  reduced_indices = tf.transpose(tf.reshape(reduced_indices, [rank, -1]))

  # Compute corresponding indices into the full FOV image.
  offsets = [tf.range(r) * s for s, r in zip(
    reduced_shape.as_list(), canonical_reduction)]
  offsets = tf.transpose(tf.reshape(
    tf.stack(tf.meshgrid(*offsets)), [rank, -1]))
  indices = tf.expand_dims(reduced_indices, -2) + offsets

  # Compute the system matrices, ie, pixel-wise sensitivity matrices folding the
  # full FOV image into a reduced FOV image.
  sens_matrix = tf.gather_nd(sensitivities, indices)
  sens_matrix = tf.transpose(
    sens_matrix, [0, 2, 1] + list(range(3, 3 + sens_batch_shape.rank)))

  # Compute the right hand sides for the set of linear systems.
  rhs = tf.gather_nd(aliased_images, reduced_indices)

  # Remove any pixels known to have zero signal, with no contributions from any
  # of the aliases. Currently we can't do this for batched sensitivities, so it
  # is disabled in that case.
  if sens_batch_shape.rank == 0:
    mask = tf.reduce_sum(tf.math.square(tf.math.abs(sens_matrix)), -2) > 0
    mask = tf.math.reduce_any(mask, axis=-1)
    sens_matrix = tf.boolean_mask(sens_matrix, mask, axis=0)
    rhs = tf.boolean_mask(rhs, mask, axis=0)
    indices = tf.boolean_mask(indices, mask, axis=0)

  # Move batch dimensions to the beginning.
  sens_matrix = tf.transpose(
    sens_matrix, list(range(3, sens_matrix.shape.rank)) + [0, 1, 2])
  rhs = tf.transpose(rhs, list(range(2, rhs.shape.rank)) + [0, 1])
  rhs = tf.expand_dims(rhs, -1)

  # Broadcast the sensitivity matrix as necessary.
  sens_matrix = tf.broadcast_to(
    sens_matrix, batch_shape + sens_matrix.shape[-3:])

  # Solve the pixel-wise linear least-squares problems.
  unfolded_values = tf.linalg.lstsq(sens_matrix, rhs,
                                    l2_regularizer=l2_regularizer,
                                    fast=fast)

  unfolded_values = tf.reshape(unfolded_values, [-1])
  output_indices = tf.reshape(indices, [-1, rank])

  # For batch mode we need to do some additional indexing calculations.
  if batch_shape.rank > 0:
    batch_size = batch_shape.num_elements()
    element_size = unfolded_values.shape[0] // batch_size

    batch_indices = tf.stack(tf.meshgrid(*[tf.range(s) for s in batch_shape]))
    batch_indices = tf.transpose(
      tf.reshape(batch_indices, [batch_shape.rank, -1]))
    batch_indices = tf.expand_dims(batch_indices, -2)
    batch_indices = tf.tile(
      batch_indices, [1] * batch_shape.rank + [element_size, 1])
    batch_indices = tf.reshape(batch_indices, [-1, batch_shape.rank])

    output_indices = tf.tile(output_indices, [batch_size, 1])
    output_indices = tf.concat([batch_indices, output_indices], -1)

  # Scatter the unfolded values into the reconstructed image.
  image = tf.scatter_nd(output_indices, unfolded_values,
                        batch_shape + image_shape)

  return image


def _cg_sense(kspace,
              trajectory,
              density=None,
              sensitivities=None,
              tol=1e-5,
              max_iter=10,
              return_cg_state=False):
  """MR image reconstruction using conjugate gradient SENSE (CG-SENSE).

  For the parameters, see `tfmri.reconstruct`.
  """
  if sensitivities is None:
    raise ValueError("Argument `sensitivities` must be specified for CG-SENSE.")

  # Inputs.
  kspace = tf.convert_to_tensor(kspace)
  sensitivities = tf.convert_to_tensor(sensitivities)
  trajectory = tf.convert_to_tensor(trajectory)

  rank = trajectory.shape[-1]
  num_points = kspace.shape[-1]
  num_coils = kspace.shape[-2]
  batch_shape = kspace.shape[:-2]
  image_shape = sensitivities.shape[-rank:]

  # Check some inputs.
  tf.debugging.assert_equal(
    tf.shape(kspace)[-1], tf.shape(trajectory)[-2], message=(
        f"The number of samples in `kspace` (axis -1) and `trajectory` "
        f"(axis -2) must match, but got: {tf.shape(kspace)[-1]}, "
        f"{tf.shape(trajectory)[-2]}"))
  tf.debugging.assert_equal(
    tf.shape(kspace)[-2], tf.shape(sensitivities)[-rank-1], message=(
        f"The number of coils in `kspace` (axis -2) and `sensitivities` "
        f"(axis {-rank-1}) must match, but got: {tf.shape(kspace)[-1]}, "
        f"{tf.shape(sensitivities)[-rank-1]}"))
  # Check batch shapes.
  kspace_batch_shape = kspace.shape[:-2]
  sens_batch_shape = sensitivities.shape[:-rank-1]
  traj_batch_shape = trajectory.shape[:-2]
  batch_shape = tf.broadcast_static_shape(kspace_batch_shape, sens_batch_shape)
  # We do not broadcast the k-space input, by design.
  if batch_shape != kspace_batch_shape:
    raise ValueError(
        f"`kspace` and `sensitivities` have incompatible batch shapes: "
        f"{kspace_batch_shape}, {sens_batch_shape}")
  batch_shape = tf.broadcast_static_shape(kspace_batch_shape, traj_batch_shape)
  if batch_shape != kspace_batch_shape:
    raise ValueError(
        f"`kspace` and `trajectory` have incompatible batch shapes: "
        f"{kspace_batch_shape}, {traj_batch_shape}")

  # For sampling density correction.
  if density is None:
    # Sampling density not provided, so estimate from trajectory.
    density = traj_ops.estimate_density(trajectory, image_shape)
  else:
    # Use the provided sampling density.
    density = tf.convert_to_tensor(density)
  density = tf.expand_dims(density, -2) # Add coil dimension.

  # For intensity correction.
  intensity = tf.math.reduce_sum(tf.math.square(tf.math.abs(sensitivities)),
                                 axis=-rank-1)

  # Prepare intensity correction linear operator.
  intensity_weights = tf.math.reciprocal_no_nan(intensity)
  linop_intensity = linalg_ops.LinearOperatorRealWeighting(
      tf.math.sqrt(intensity_weights),
      arg_shape=intensity_weights.shape[-rank:],
      dtype=kspace.dtype)

  # Prepare density compensation linear operator.
  density_weights = tf.math.reciprocal_no_nan(density)
  linop_density = linalg_ops.LinearOperatorRealWeighting(
      tf.math.sqrt(density_weights),
      arg_shape=[num_coils, num_points],
      dtype=kspace.dtype)

  # Get non-Cartesian parallel MRI operator.
  linop_parallel_mri = linalg_ops.LinearOperatorParallelMRI(
      sensitivities, trajectory=trajectory)

  # Calculate the right half of the system operator. Then, the left half is the
  # adjoint of the right half.
  linop_right = tf.linalg.LinearOperatorComposition(
      [linop_density, linop_parallel_mri, linop_intensity])
  linop_left = linop_right.H

  # Finally, make system operator. We know this to be self-adjoint and positive
  # definite, as required for CG.
  operator = tf.linalg.LinearOperatorComposition(
      [linop_left, linop_right],
      is_self_adjoint=True, is_positive_definite=True)

  # Step 1. Compute the right hand side of the linear system.
  kspace_vec = tf.reshape(kspace, batch_shape.as_list() + [-1])
  rhs = tf.linalg.matvec(linop_left,
                         tf.linalg.matvec(linop_density, kspace_vec))

  # Step 2. Perform CG iteration to solve modified system.
  result = linalg_ops.conjugate_gradient(operator, rhs,
                                         tol=tol, max_iter=max_iter)

  # Step 3. Correct intensity to obtain solution to original system.
  image_vec = tf.linalg.matvec(linop_intensity, result.x)

  # Restore image shape.
  image = tf.reshape(image_vec, batch_shape.as_list() + image_shape)

  return (image, result) if return_cg_state else image


def _grappa(kspace,
            mask=None,
            calib=None,
            sensitivities=None,
            kernel_size=5,
            weights_l2_regularizer=0.0,
            combine_coils=True,
            return_kspace=False):
  """MR image reconstruction using GRAPPA.

  For the parameters, see `tfmri.reconstruct`.
  """
  if mask is None:
    raise ValueError("Argument `mask` must be provided.")
  if calib is None:
    raise ValueError("Argument `calib` must be provided.")

  kspace = tf.convert_to_tensor(kspace)
  calib = tf.convert_to_tensor(calib)
  mask = tf.convert_to_tensor(mask)

  # If mask has no holes, there is nothing to do.
  if tf.math.count_nonzero(tf.math.logical_not(mask)) == 0:
    return kspace

  # Use `mask` to infer rank.
  rank = mask.shape.rank

  # If an `int` was given for the kernel size, use isotropic kernel in all
  # dimensions.
  if isinstance(kernel_size, int):
    kernel_size = [kernel_size] * rank

  # Get multi-dimensional and flat indices for kernel center, e.g. [2, 2]
  # (multi), 12 (flat) for [5, 5] kernel. `kernel_center` is also used as half
  # the size of the kernel.
  kernel_center = [ks // 2 for ks in kernel_size]
  kernel_center_index = array_ops.ravel_multi_index(kernel_center, kernel_size)

  # Save batch shape for later, broadcast `calib` to match `kspace` and reshape
  # inputs to a single batch axis (except `mask`, which should have no batch
  # dimensions).
  kspace_shape = tf.shape(kspace)[-rank-1:] # No batch dims.
  calib_shape = tf.shape(calib)[-rank-1:] # No batch dims.
  batch_shape = tf.shape(kspace)[:-rank-1]
  if tf.math.reduce_prod(tf.shape(calib)[:-rank-1]) == 1:
    # Shared calibration. Do not broadcast, but maybe add batch dimension.
    calib = tf.reshape(calib, tf.concat([[1], calib_shape], 0))
  else:
    # General case. Calibration may not be shared for all inputs.
    calib = tf.broadcast_to(calib, tf.concat([batch_shape, calib_shape], 0))
  kspace = tf.reshape(kspace, tf.concat([[-1], kspace_shape], 0))
  calib = tf.reshape(calib, tf.concat([[-1], calib_shape], 0))
  batch_size = tf.shape(kspace)[0]
  num_coils = tf.shape(kspace)[1]

  # Move coil axis to the end, i.e. [batch, coil, *dims] -> [batch, *dims, coil]
  perm = [0, *list(range(2, rank + 2)), 1]
  kspace = tf.transpose(kspace, perm)
  calib = tf.transpose(calib, perm)

  # Initialize output tensor and fill with the measured values.
  full_shape = tf.concat([[batch_size], tf.shape(mask), [num_coils]], 0)
  measured_indices = tf.cast(tf.where(mask), tf.int32)
  measured_indices = _insert_batch_indices(measured_indices, batch_size)
  full_kspace = tf.scatter_nd(measured_indices,
                              tf.reshape(kspace, [-1, num_coils]),
                              full_shape)

  # Pad arrays so we can slide the kernel in the edges.
  paddings = tf.concat([[0], kernel_center, [0]], 0)
  paddings = tf.expand_dims(paddings, -1)
  paddings = tf.tile(paddings, [1, 2])
  full_kspace = tf.pad(full_kspace, paddings) # pylint:disable=no-value-for-parameter
  calib = tf.pad(calib, paddings) # pylint:disable=no-value-for-parameter
  mask = tf.pad(mask, paddings[1:-1, :], constant_values=False)

  # Extract all patches from the mask. We cast to `float32` because `bool` is
  # not currently supported in all devices for `_extract_patches` (TF v2.6).
  mask_patches = _extract_patches(
      tf.cast(mask[tf.newaxis, ..., tf.newaxis], tf.float32), kernel_size) > 0.5

  # Find the unique patterns among all the mask patches. `unique_inverse` are
  # the indices that reconstruct `mask_patches` from `unique_patches`.
  patch_array_shape = tf.shape(mask_patches, out_type=tf.int64)[1:-1]
  mask_patches = tf.reshape(
      mask_patches, [-1, tf.math.reduce_prod(kernel_size)])
  unique_patches, unique_inverse = tf.raw_ops.UniqueV2(x=mask_patches, axis=[0])
  unique_inverse = tf.cast(unique_inverse, tf.int64)
  unique_inverse = tf.reshape(unique_inverse, patch_array_shape)

  # Select only patches that:
  # - Have a hole in the center. Otherwise job is done!
  # - Are not empty. Otherwise there is nothing we can do!
  valid_patch_indices = tf.where(tf.math.logical_and(
      tf.math.logical_not(unique_patches[:, kernel_center_index]),
      tf.math.reduce_any(unique_patches, axis=-1)))
  valid_patch_indices = tf.squeeze(valid_patch_indices, axis=-1)

  # Get all overlapping patches of ACS.
  calib_patches = _extract_patches(calib, kernel_size)
  calib_patches = _flatten_spatial_axes(calib_patches)
  calib_patches = _split_last_dimension(calib_patches, num_coils)

  # For each geometry.
  for patch_index in valid_patch_indices:

    # Estimate the GRAPPA weights for current geometry. Get all possible
    # calibration patches with current geometry: sources (available data) and
    # targets (holes to fill). Given known sources and targets, estimate weights
    # using (possibly regularized) least squares.
    sources = tf.boolean_mask(calib_patches,
                              unique_patches[patch_index, :], axis=-2)
    sources = _flatten_last_dimensions(sources)
    targets = calib_patches[..., kernel_center_index, :]
    weights = tf.linalg.lstsq(sources, targets,
                              l2_regularizer=weights_l2_regularizer)

    # Now find all patch offsets (upper-left corners) and centers for current
    # geometry.
    patch_offsets = tf.where(unique_inverse == patch_index)
    patch_centers = tf.cast(patch_offsets + kernel_center, tf.int32)
    patch_centers = _insert_batch_indices(patch_centers, batch_size)

    # Collect all sources from partially measured `kspace` (all patches with
    # current geometry are pulled at the same time here).
    sources = image_ops.extract_glimpses(
        full_kspace, kernel_size, patch_offsets)
    sources = _split_last_dimension(sources, num_coils)
    sources = tf.boolean_mask(sources, unique_patches[patch_index, :], axis=-2)
    sources = _flatten_last_dimensions(sources)

    # Compute targets using the previously estimated weights.
    targets = tf.linalg.matmul(sources, weights)
    targets = tf.reshape(targets, [-1, num_coils])

    # Fill the holes.
    full_kspace = tf.tensor_scatter_nd_update(full_kspace,
                                              patch_centers,
                                              targets)

  # `full_kspace` was zero-padded at the beginning. Crop it to correct shape.
  full_kspace = image_ops.central_crop(
      full_kspace, tf.concat([[-1], full_shape[1:-1], [-1]], 0))

  # Move coil axis back. [batch, *dims, coil] -> [batch, coil, *dims]
  inv_perm = tf.math.invert_permutation(perm)
  full_kspace = tf.transpose(full_kspace, inv_perm)

  # Restore batch shape.
  result = tf.reshape(
      full_kspace, tf.concat([batch_shape, tf.shape(full_kspace)[1:]], 0))

  if return_kspace:
    return result

  # Inverse FFT to image domain.
  result = fft_ops.ifftn(result, axes=list(range(-rank, 0)), shift=True)

  # Combine coils if requested.
  if combine_coils:
    result = coil_ops.combine_coils(result,
                                    maps=sensitivities,
                                    coil_axis=-rank-1)

  return result


def _pics(kspace,
          mask=None,
          trajectory=None,
          density=None,
          sensitivities=None,
          recon_shape=None,
          rank=None,
          regularizers=None,
          optimizer=None,
          initial_image=None,
          max_iterations=50,
          use_density_compensation=True):
  """MR image reconstruction using parallel imaging and compressed sensing.

  For the parameters, see `tfmri.reconstruct`.
  """
  # Check reconstruction shape.
  if recon_shape is None:
    raise ValueError(
        "Input `recon_shape` must be provided for CS.")
  recon_shape = tf.TensorShape(recon_shape)

  # Check regularizers.
  if regularizers is None:
    regularizers = []

  # Check optimizer.
  if optimizer is None:
    optimizer = 'lbfgs' # Default optimizer.
  optimizer = check_util.validate_enum(optimizer, {'lbfgs'}, name='optimizer')

  # Check what kind of reconstruction this is.
  is_cartesian = trajectory is None
  is_multicoil = sensitivities is not None

  if is_cartesian: # Cartesian imaging.
    # Number of spatial dimensions. Use `rank` parameter. If `rank` was not
    # provided, assume all dimensions are spatial dimensions.
    rank = rank or recon_shape.rank
    # Number of dimensions in reconstruction (spatial dimensions plus other
    # potentially regularized dimensions such as time).
    recon_dims = recon_shape.rank
    time_dims = recon_dims - rank
    # Shape of `kspace` (encoding dimensions only). Shape has length N for
    # N-dimensional imaging, or N + 1 for multicoil imaging.
    kspace_encoding_shape = kspace.shape[-(rank + is_multicoil):]
    # The batch shape. The shape of `kspace` without the encoding dimensions,
    # time dimensions or coil dimension.
    batch_shape = kspace.shape[:-(recon_dims + is_multicoil)]

  else: # Non-Cartesian imaging.
    # Infer rank from trajectory. Parameter `rank` is ignored for non-Cartesian
    # imaging.
    rank = trajectory.shape[-1]
    # Number of dimensions in reconstruction (spatial dimensions plus other
    # potentially regularized dimensions such as time).
    recon_dims = recon_shape.rank
    time_dims = recon_dims - rank
    # Shape of `kspace` (encoding dimensions only). Shape has length 1, or 2 for
    # multicoil imaging.
    kspace_encoding_shape = kspace.shape[-(1 + is_multicoil):]
    # The batch shape. The shape of `kspace` without the single encoding
    # dimension, time dimensions or coil dimension.
    batch_shape = kspace.shape[:-(time_dims + 1 + is_multicoil)]

  # Subshapes of reconstruction shape. `image_shape` has the spatial dimensions,
  # while `time_shape` has the time dimensions (or any other non-spatial
  # dimensions).
  image_shape = recon_shape[-rank:] # pylint: disable=invalid-unary-operand-type
  time_shape = recon_shape[:-rank] # pylint: disable=invalid-unary-operand-type

  # The solution `x` should have shape `recon_shape` plus the additional batch
  # dimensions. The measurements `y` should be the flattened encoding
  # dimension/s plus the time dimensions plus the batch dimensions.
  x_shape = batch_shape + recon_shape
  y_shape_tensor = tf.concat([batch_shape, time_shape, [-1]], 0)

  # Estimate density if it was not provided.
  if not is_cartesian and density is None and use_density_compensation:
    density = traj_ops.estimate_density(trajectory, image_shape)

  # Compute and apply weights.
  if not is_cartesian and use_density_compensation:
    weights = tf.math.sqrt(tf.math.reciprocal_no_nan(density))
    if is_multicoil:
      weights = tf.expand_dims(weights, -2) # Add the channel dimension.
    kspace *= tf.cast(weights, kspace.dtype)

  # Flatten `kspace` to a single encoding dimension.
  y = tf.reshape(kspace, y_shape_tensor)

  # Select encoding operator.
  if is_multicoil:
    e = linalg_ops.LinearOperatorParallelMRI(
        sensitivities,
        mask=mask,
        trajectory=trajectory,
        rank=recon_shape.rank,
        norm='ortho')
  else:
    if is_cartesian:
      e = linalg_ops.LinearOperatorFFT(recon_shape, mask=mask, norm='ortho')
    else:
      e = linalg_ops.LinearOperatorNUFFT(recon_shape, trajectory, norm='ortho')

  # Add density compensation to encoding operator.
  if not is_cartesian and use_density_compensation:
    linop_dens = linalg_ops.LinearOperatorRealWeighting(
        weights,
        arg_shape=kspace_encoding_shape,
        dtype=kspace.dtype)
    e = tf.linalg.LinearOperatorComposition([linop_dens, e])

  @tf.function
  @math_ops.make_val_and_grad_fn
  def _objective(x):
    # Reinterpret real input as complex and reshape to correct shape.
    x = math_ops.view_as_complex(x, stacked=False)
    x = tf.reshape(x, y_shape_tensor)

    # Compute data consistency terms.
    value = tf.math.abs(tf.norm(y - tf.linalg.matvec(e, x), ord=2))

    # Add regularization term[s].
    x = tf.reshape(x, x_shape)
    for reg in regularizers:
      value += reg(x)

    return value

  # Prepare initial estimate.
  if initial_image is None:
    initial_image = tf.linalg.matvec(e.H, y)
  initial_image = tf.reshape(initial_image,
                             tf.concat([batch_shape, [-1]], 0))
  initial_image = math_ops.view_as_real(initial_image, stacked=False)

  # Perform optimization.
  if optimizer == 'lbfgs':
    result = optimizer_ops.lbfgs_minimize(_objective, initial_image,
                                          max_iterations=max_iterations)
  else:
    raise ValueError(f"Unknown optimizer: {optimizer}")

  # Image to correct shape and type.
  recon = tf.reshape(
      math_ops.view_as_complex(result.position, stacked=False), recon_shape)

  return recon


def reconstruct_adj(kspace,
                    image_shape,
                    mask=None,
                    trajectory=None,
                    density=None,
                    sensitivities=None,
                    phase=None,
                    sens_norm=True):
  """Reconstructs an image using the adjoint MRI operator."""
  kspace = tf.convert_to_tensor(kspace)

  # Create the linear operator.
  operator = linalg_ops.LinearOperatorMRI(image_shape,
                                          mask=mask,
                                          trajectory=trajectory,
                                          density=density,
                                          sensitivities=sensitivities,
                                          phase=phase,
                                          fft_norm='ortho',
                                          sens_norm=sens_norm)
  rank = operator.image_rank

  # Apply density compensation, if provided.
  if density is not None:
    dens_weights_sqrt = tf.math.sqrt(tf.math.reciprocal_no_nan(density))
    dens_weights_sqrt = tf.cast(dens_weights_sqrt, kspace.dtype)
    if operator.is_multicoil:
      dens_weights_sqrt = tf.expand_dims(dens_weights_sqrt, axis=-2)
    kspace *= dens_weights_sqrt

  # Compute zero-filled image using the adjoint operator.
  image = operator.H.transform(kspace)

  # Apply intensity correction, if requested.
  if operator.is_multicoil and sens_norm:
    sens_weights_sqrt = tf.math.reciprocal_no_nan(
        tf.norm(sensitivities, axis=-(rank + 1), keepdims=True))
    image *= sens_weights_sqrt

  return image


def reconstruct_cgsense(kspace,
                        image_shape,
                        mask=None,
                        trajectory=None,
                        density=None,
                        sensitivities=None,
                        phase=None,
                        sens_norm=True,
                        reg_parameter=None,
                        reg_prior=None,
                        filter_kspace=None,
                        tolerance=1e-5,
                        max_iterations=50,
                        custom_gradient=False):
  # We don't do a lot of input checking here, since it will be done by the
  # operator.
  kspace = tf.convert_to_tensor(kspace)
  if reg_parameter is not None:
    reg_parameter = tf.cast(reg_parameter, dtype=kspace.dtype)
  if reg_prior is None:
    reg_prior = tf.constant(0.0, dtype=kspace.dtype)

  # Create the linear operator.
  operator = linalg_ops.LinearOperatorGramMatrix(
      linalg_ops.LinearOperatorMRI(image_shape,
                                   mask=mask,
                                   trajectory=trajectory,
                                   density=density,
                                   sensitivities=sensitivities,
                                   phase=phase,
                                   fft_norm='ortho',
                                   sens_norm=sens_norm),
      reg_parameter=reg_parameter)
  rank = operator.operator.image_rank

  # Apply density compensation, if provided.
  if density is not None:
    kspace *= operator.operator._dens_weights_sqrt

  # Compute the right-hand side.
  rhs = operator.operator.H.transform(kspace)

  # Add regularization prior to right-hand side, if needed.
  if reg_parameter is not None:
    rhs += reg_parameter * reg_prior

  # Run CG iterations.
  def _conjugate_gradient(x):
    state = linalg_ops.conjugate_gradient(operator, x,
                                          tol=tolerance,
                                          max_iter=max_iterations)
    return state.x

  @tf.custom_gradient
  def _conjugate_gradient_custom_grad(x):
    y = _conjugate_gradient(x)
    def grad(dy):
      return _conjugate_gradient(dy)
    return y, grad
  
  if custom_gradient:
    image = _conjugate_gradient_custom_grad(rhs)
  else:
    image = _conjugate_gradient(rhs)

  # Apply intensity correction, if requested.
  if operator.operator.is_multicoil and sens_norm:
    sens_weights_sqrt = tf.math.reciprocal_no_nan(
        tf.norm(sensitivities, axis=-(rank + 1), keepdims=True))
    image *= sens_weights_sqrt

  # If necessary, filter the image to remove k-space corners. This should be
  # done if the trajectory has circular coverage and does not cover the k-space
  # corners. If the user has not specified whether to apply the filter, we do it
  # only for non-Cartesian trajectories, under the assumption that non-Cartesian
  # trajectories are likely to have circular coverage of k-space while Cartesian
  # trajectories are likely to have rectangular coverage.
  if filter_kspace is None:
    is_probably_circular = operator.operator.is_non_cartesian
    filter_kspace = is_probably_circular
  if filter_kspace:
    fft_axes = list(range(-rank, 0))
    kspace = fft_ops.fftn(image, axes=fft_axes, norm='ortho', shift=True)
    kspace = signal_ops.filter_kspace(kspace, rank=rank, filter_type='atanfilt')
    image = fft_ops.ifftn(kspace, axes=fft_axes, norm='ortho', shift=True)

  return image


def reconstruct_lstsq(kspace,
                      image_shape,
                      mask=None,
                      trajectory=None,
                      density=None,
                      sensitivities=None,
                      phase=None,
                      sens_norm=True,
                      regularizer=None,
                      initial_image=None,
                      optimizer=None,
                      optimizer_kwargs=None,
                      image_rank=None):
  """Reconstructs an image using a least-squares formulation."""
  # Check optimizer.
  optimizer = check_util.validate_enum(
      optimizer or 'admm', {'admm', 'lbfgs'}, name='optimizer')

  # We don't do a lot of input checking here, since it will be done by the
  # operator.
  kspace = tf.convert_to_tensor(kspace)

  # Create the linear operator.
  operator = linalg_ops.LinearOperatorMRI(image_shape,
                                          mask=mask,
                                          trajectory=trajectory,
                                          density=density,
                                          sensitivities=sensitivities,
                                          fft_norm='ortho',
                                          sens_norm=sens_norm,
                                          image_rank=image_rank)
  image_rank = operator.image_rank

  # Apply density compensation, if provided.
  if density is not None:
    kspace *= operator._dens_weights_sqrt

  if optimizer == 'admm':
    result = optimizer_ops.admm_minimize(
        convex_ops.ConvexFunctionLeastSquares(operator, kspace),
        regularizer.convex_function,
        operator_a=regularizer.linear_operator,
        **optimizer_kwargs)
    image = operator.expand_domain_dimension(result.x)

  elif optimizer == 'lbfgs':
    # Prepare initial estimate.
    y = operator.flatten_range_shape(kspace)
    if initial_image is None:
      initial_image = operator.matvec(y, adjoint=True)
    # Currently L-BFGS implementation only supports real numbers, so reinterpret
    # complex image as real (C^N -> R^2*N).
    initial_image = math_ops.view_as_real(initial_image, stacked=False)    

    # Define the objective function and its gradient.
    @tf.function
    @math_ops.make_val_and_grad_fn
    def _objective(x):
      # Reinterpret real input as complex and reshape to correct shape.
      x = math_ops.view_as_complex(x, stacked=False)
      # Compute data consistency and regularization terms and add.
      dc_term = tf.math.abs(tf.norm(y - operator.matvec(x), ord=2))
      reg_term = regularizer(x)
      return dc_term + reg_term

    # Do minimization.
    result = optimizer_ops.lbfgs_minimize(_objective, initial_image,
                                          **optimizer_kwargs)

    # Reinterpret real result as complex and reshape image.
    image = operator.expand_domain_dimension(
        math_ops.view_as_complex(result.position, stacked=False))

  else:
    raise ValueError(f"Unknown optimizer: {optimizer}")

  # Apply intensity correction, if requested.
  if operator.is_multicoil and sens_norm:
    sens_weights_sqrt = tf.math.reciprocal_no_nan(
        tf.norm(sensitivities, axis=-(image_rank + 1), keepdims=True))
    image *= sens_weights_sqrt

  return image


def _extract_patches(images, sizes):
  """Extract patches from N-D image.

  Args:
    images: A `Tensor` of shape `[batch_size, *spatial_dims, channels]`.
      `spatial_dims` must have rank 2 or 3.
    sizes: A list of `ints`. The size of the patches. Must have the same length
      as `spatial_dims`.

  Returns:
    A `Tensor` containing the extracted patches.

  Raises:
    ValueError: If rank is not 2 or 3.
  """
  rank = len(sizes)
  if rank == 2:
    patches = tf.image.extract_patches(
        images,
        sizes=[1, *sizes, 1],
        strides=[1, 1, 1, 1],
        rates=[1, 1, 1, 1],
        padding='VALID')
  elif rank == 3:
    # `tf.extract_volume_patches` does not support complex tensors, so we do the
    # extraction for real and imaginary separately and then combine.
    if images.dtype.is_complex:
      patches_real = tf.extract_volume_patches(
          tf.math.real(images),
          ksizes=[1, *sizes, 1],
          strides=[1, 1, 1, 1, 1],
          padding='VALID')
      patches_imag = tf.extract_volume_patches(
          tf.math.imag(images),
          ksizes=[1, *sizes, 1],
          strides=[1, 1, 1, 1, 1],
          padding='VALID')
      patches = tf.dtypes.complex(patches_real, patches_imag)
    else:
      patches = tf.extract_volume_patches(
          images,
          ksizes=[1, *sizes, 1],
          strides=[1, 1, 1, 1, 1],
          padding='VALID')
  else:
    raise ValueError(f"Unsupported rank: {rank}")
  return patches


def _insert_batch_indices(indices, batch_size): # pylint: disable=missing-param-doc
  """Inserts batch indices into an array of indices.

  Given an array of indices with shape `[M, N]` which indexes into a tensor `x`,
  returns a new array with shape `[batch_size * M, N + 1]` which indexes into a
  tensor of shape `[batch_size] + x.shape`.
  """
  batch_indices = tf.expand_dims(tf.repeat(
      tf.range(batch_size), tf.shape(indices)[0]), -1)
  indices = tf.tile(indices, [batch_size, 1])
  indices = tf.concat([batch_indices, indices], -1)
  return indices


def _flatten_spatial_axes(images): # pylint: disable=missing-param-doc
  """Flatten the spatial axes of an image.

  If `images` has shape `[batch_size, *spatial_dims, channels]`, returns a
  `Tensor` with shape `[batch_size, prod(spatial_dims), channels]`.
  """
  shape = tf.shape(images)
  return tf.reshape(images, [shape[0], -1, shape[-1]])


def _split_last_dimension(x, size):
  """Splits the last dimension into two dimensions.

  Returns an array of rank `tf.rank(x) + 1` whose last dimension has size
  `size`.
  """
  return tf.reshape(x, tf.concat([tf.shape(x)[:-1], [-1, size]], 0))


def _flatten_last_dimensions(x):
  """Flattens the last two dimensions.

  Returns an array of rank `tf.rank(x) - 1`.
  """
  return tf.reshape(x, tf.concat([tf.shape(x)[:-2], [-1]], 0))


def _select_reconstruction_method(kspace, # pylint: disable=unused-argument
                                  mask,
                                  trajectory,
                                  density,
                                  calib,
                                  sensitivities,
                                  method):
  """Select an appropriate reconstruction method based on user inputs.

  For the parameters, see `tfmri.reconstruct`.
  """
  # If user selected a method, use it. We do not check that inputs are valid
  # here, this will be done by the methods themselves.
  if method is not None:
    if method not in _MR_RECON_METHODS:
      return ValueError(
        f"Could not find a reconstruction method named: `{method}`")
    return method

  # No method was specified: choose a default one.
  if (sensitivities is None and
      trajectory is None and
      density is None and
      calib is None and
      mask is None):
    return 'fft'

  if (sensitivities is None and
      trajectory is not None and
      calib is None and
      mask is None):
    return 'nufft'

  if (sensitivities is not None and
      trajectory is None and
      density is None and
      calib is None and
      mask is None):
    return 'sense'

  if (sensitivities is not None and
      trajectory is not None and
      calib is None and
      mask is None):
    return 'cg_sense'

  if (trajectory is None and
      density is None and
      calib is not None and
      mask is not None):
    return 'grappa'

  # Nothing worked.
  raise ValueError(
    "Could not find any reconstruction method that supports the specified "
    "combination of inputs.")


def reconstruct_partial_kspace(kspace,
                               factors,
                               return_complex=False,
                               return_kspace=False,
                               method='zerofill',
                               **kwargs):
  """Partial Fourier image reconstruction.

  Args:
    kspace: A `Tensor`. The *k*-space data. Must have type `complex64` or
      `complex128`. Must have shape `[..., *K]`, where `K` are the spatial
      frequency dimensions. `kspace` should only contain the observed data,
      without zero-filling of any kind.
    factors: A list of `floats`. The partial Fourier factors. There must be a
      factor for each spatial frequency dimension. Each factor must be between
      0.5 and 1.0 and indicates the proportion of observed *k*-space values
      along the specified dimensions.
    return_complex: A `bool`. If `True`, returns complex instead of real-valued
      images. Note that partial Fourier reconstruction assumes that images are
      real, and the returned complex values may not be valid in all contexts.
    return_kspace: A `bool`. If `True`, returns the filled *k*-space instead of
      the reconstructed images. This is always complex-valued.
    method: A `string`. The partial Fourier reconstruction algorithm. Must be
      one of `"zerofill"`, `"homodyne"` (homodyne detection method) or `"pocs"`
      (projection onto convex sets method).
    **kwargs: Additional method-specific keyword arguments. See Notes for
    details.

  Returns:
    A `Tensor` with shape `[..., *S]` where `S = K / factors`. Has type
    `kspace.dtype` if either `return_complex` or `return_kspace` is `True`, and
    type `kspace.dtype.real_dtype` otherwise.

  Notes:
    This function accepts some method-specific arguments:

    * `method="zerofill"` accepts no additional arguments.

    * `method="homodyne"` accepts the following additional keyword arguments:

      * **weighting_fn**: An optional `string`. The weighting function. Must be
        one of `"step"`, `"ramp"`. Defaults to `"ramp"`. `"ramp"` helps
        mitigate Gibbs artifact, while `"step"` has better SNR properties.

    * `method="pocs"` accepts the following additional keyword arguments:

      * **tol**: An optional `float`. The convergence tolerance. Defaults to
        `1e-5`.
      * **max_iter**: An optional `int`. The maximum number of iterations of the
        POCS algorithm. Defaults to `10`.

  References:
    .. [1] Noll, D. C., Nishimura, D. G., & Macovski, A. (1991). Homodyne
      detection in magnetic resonance imaging. IEEE transactions on medical
      imaging, 10(2), 154-163.
    .. [2] Haacke, E. M., Lindskogj, E. D., & Lin, W. (1991). A fast, iterative,
      partial-Fourier technique capable of local phase recovery. Journal of
      Magnetic Resonance (1969), 92(1), 126-145.
  """
  kspace = tf.convert_to_tensor(kspace)
  factors = tf.convert_to_tensor(factors)

  # Validate inputs.
  method = check_util.validate_enum(method, {'zerofill', 'homodyne', 'pocs'})
  tf.debugging.assert_greater_equal(factors, 0.5, message=(
    f"`factors` must be greater than or equal to 0.5, but got: {factors}"))
  tf.debugging.assert_less_equal(factors, 1.0, message=(
    f"`factors` must be less than or equal to 1.0, but got: {factors}"))

  func = {'zerofill': _pf_zerofill,
          'homodyne': _pf_homodyne,
          'pocs': _pf_pocs}

  return func[method](kspace, factors,
                      return_complex=return_complex,
                      return_kspace=return_kspace,
                      **kwargs)


def _pf_zerofill(kspace, factors, return_complex=False, return_kspace=False):
  """Partial Fourier reconstruction using zero-filling.

  For the parameters, see `reconstruct_partial_kspace`.
  """
  output_shape = _scale_shape(tf.shape(kspace), 1.0 / factors)
  paddings = tf.expand_dims(output_shape - tf.shape(kspace), -1)
  paddings = tf.pad(paddings, [[0, 0], [1, 0]]) # pylint: disable=no-value-for-parameter
  full_kspace = tf.pad(kspace, paddings) # pylint: disable=no-value-for-parameter

  if return_kspace:
    return full_kspace
  image = _ifftn(full_kspace, tf.size(factors))
  if return_complex:
    return image
  return tf.math.abs(image)


def _pf_homodyne(kspace,
                 factors,
                 return_complex=False,
                 return_kspace=False,
                 weighting_fn='ramp'):
  """Partial Fourier reconstruction using homodyne detection.

  For the parameters, see `reconstruct_partial_kspace`.
  """
  # Rank of this operation.
  dtype = kspace.dtype

  # Create zero-filled k-space.
  full_kspace = _pf_zerofill(kspace, factors, return_kspace=True)
  full_shape = tf.shape(full_kspace)

  # Shape of the symmetric region.
  shape_sym = _scale_shape(full_shape, 2.0 * (factors - 0.5))

  # Compute weighting function. Weighting function is:
  # - 2.0 for the asymmetric part of the measured k-space.
  # - A ramp from 2.0 to 0.0 for the symmetric part of the measured k-space.
  # - 0.0 for the part of k-space that was not measured.
  weights = tf.constant(1.0, dtype=kspace.dtype)
  for i in range(len(factors)): #reverse_axis, factor in enumerate(tf.reverse(factors, [0])):
    dim_sym = shape_sym[-i-1]
    dim_asym = (full_shape[-i-1] - dim_sym) // 2
    # Weighting for symmetric part of k-space.
    if weighting_fn == 'step':
      weights_sym = tf.ones([dim_sym], dtype=dtype)
    elif weighting_fn == 'ramp':
      weights_sym = tf.cast(tf.linspace(2.0, 0.0, dim_sym), dtype)
    else:
      raise ValueError(f"Unknown `weighting_fn`: {weighting_fn}")
    weights *= tf.reshape(tf.concat(
        [2.0 * tf.ones([dim_asym], dtype=dtype),
         weights_sym,
         tf.zeros([dim_asym], dtype=dtype)], 0), [-1] + [1] * i)

  # Phase correction. Estimate a phase modulator from low resolution image using
  # symmetric part of k-space.
  phase_modulator = _estimate_phase_modulator(full_kspace, factors)

  # Compute image with following steps.
  # 1. Apply weighting function.
  # 2. Convert to image domain.
  # 3. Apply phase correction.
  full_kspace *= weights
  image = _ifftn(full_kspace, tf.size(factors))
  image *= tf.math.conj(phase_modulator)

  if return_kspace:
    return _fftn(image, tf.size(factors))
  if return_complex:
    return image
  return _real_non_negative(image)


def _pf_pocs(kspace,
             factors,
             return_complex=False,
             return_kspace=False,
             max_iter=10,
             tol=1e-5):
  """Partial Fourier reconstruction using projection onto convex sets (POCS).

  For the parameters, see `reconstruct_partial_kspace`.
  """
  # Zero-filled k-space.
  full_kspace = _pf_zerofill(kspace, factors, return_kspace=True)

  # Generate a k-space mask which is True for measured samples, False otherwise.
  kspace_mask = tf.constant(True)
  # for i, factor in enumerate(tf.reverse(factors, [0])):
  for i in tf.range(tf.size(factors)):
    dim_partial = kspace.shape[-i-1]
    dim_full = full_kspace.shape[-i-1]
    kspace_mask = tf.math.logical_and(kspace_mask, tf.reshape(tf.concat(
        [tf.fill([dim_partial], True),
         tf.fill([dim_full - dim_partial], False)], 0),
            tf.concat([[-1], tf.repeat([1], [i])], 0)))

  # Estimate the phase modulator from central symmetric region of k-space.
  phase_modulator = _estimate_phase_modulator(full_kspace, factors)

  # Initial estimate of the solution.
  image = tf.zeros_like(full_kspace)

  # Type to hold state of the iteration.
  pocs_state = collections.namedtuple('pocs_state', ['i', 'x', 'r'])

  def stopping_criterion(i, state):
    return tf.math.logical_and(i < max_iter,
                               state.r > tol)

  def pocs_step(i, state):
    prev = state.x
    # Set the estimated phase.
    image = tf.cast(tf.math.abs(prev), prev.dtype) * phase_modulator
    # Data consistency. Replace estimated k-space values by measured ones if
    # available.
    kspace = _fftn(image, tf.size(factors))
    kspace = tf.where(kspace_mask, full_kspace, kspace)
    image = _ifftn(kspace, tf.size(factors))
    # Phase demodulation.
    image *= tf.math.conj(phase_modulator)
    # Calculate the relative difference.
    diff = tf.math.abs(tf.norm(image - prev) / tf.norm(prev))
    return i + 1, pocs_state(i=i + 1, x=image, r=diff)

  i = tf.constant(0, dtype=tf.int32)
  state = pocs_state(i=0, x=image, r=1.0)
  _, state = tf.while_loop(stopping_criterion, pocs_step, [i, state])

  image = state.x
  if return_kspace:
    return _fftn(image, tf.size(factors))
  if return_complex:
    return image
  return _real_non_negative(image)


def _estimate_phase_modulator(full_kspace, factors): # pylint: disable=missing-param-doc
  """Estimate a phase modulator from central region of k-space."""
  shape_sym = _scale_shape(tf.shape(full_kspace), 2.0 * (factors - 0.5))
  paddings = tf.expand_dims((tf.shape(full_kspace) - shape_sym) // 2, -1)
  paddings = tf.tile(paddings, [1, 2])
  symmetric_mask = tf.pad(tf.ones(shape_sym, dtype=full_kspace.dtype), paddings) # pylint: disable=no-value-for-parameter
  symmetric_kspace = full_kspace * symmetric_mask
  ref_image = _ifftn(symmetric_kspace, tf.size(factors))
  phase_modulator = tf.math.exp(tf.dtypes.complex(
      tf.constant(0.0, dtype=ref_image.dtype.real_dtype),
      tf.math.angle(ref_image)))
  return phase_modulator


def _scale_shape(shape, factors):
  """Scale the last dimensions of `shape` by `factors`."""
  factors = tf.pad(factors, [[tf.size(shape) - tf.size(factors), 0]],
                   constant_values=1.0)
  return tf.cast(tf.cast(shape, tf.float32) * factors + 0.5, tf.int32)


_real_non_negative = lambda x: tf.math.maximum(0.0, tf.math.real(x))


_fftn = lambda x, rank: fft_ops.fftn(x, axes=tf.range(-rank, 0), shift=True)
_ifftn = lambda x, rank: fft_ops.ifftn(x, axes=tf.range(-rank, 0), shift=True)


_MR_RECON_METHODS = {
  'fft': _fft,
  'nufft': _nufft,
  'inufft': _inufft,
  'sense': _sense,
  'cg_sense': _cg_sense,
  'grappa': _grappa,
  'pics': _pics
}
