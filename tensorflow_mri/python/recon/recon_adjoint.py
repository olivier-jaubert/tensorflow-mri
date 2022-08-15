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
"""Signal reconstruction (adjoint)."""

import tensorflow as tf

from tensorflow_mri.python.util import api_util
from tensorflow_mri.python.linalg import linear_operator_mri


@api_util.export("recon.custom_adjoint")
def recon_adjoint(data, operator):
  r"""Reconstructs a signal using the adjoint of the system operator.

  Given measurement data :math:`b` generated by a linear system :math:`A` such
  that :math:`Ax = b`, this function estimates the corresponding signal
  :math:`x` as :math:`x = A^H b`, where :math:`A` is the specified linear
  operator.

  Args:
    data: A `tf.Tensor` of real or complex dtype. The measured data.
    operator: A `tfmri.linalg.LinearOperator` representing the system operator.

  Returns:
    A `tf.Tensor` with the same dtype as `data`. The reconstructed signal.
  """
  data = tf.convert_to_tensor(data)
  data = operator.preprocess(data, adjoint=True)
  signal = operator.transform(data, adjoint=True)
  signal = operator.postprocess(signal, adjoint=True)
  return signal


@api_util.export("recon.adjoint", "recon.adj")
def recon_adjoint_mri(kspace,
                     image_shape,
                     mask=None,
                     trajectory=None,
                     density=None,
                     sensitivities=None,
                     phase=None,
                     sens_norm=True):
  r"""Reconstructs an MR image using the adjoint MRI operator.

  Given *k*-space data :math:`b`, this function estimates the corresponding
  image as :math:`x = A^H b`, where :math:`A` is the MRI linear operator.

  This operator supports Cartesian and non-Cartesian *k*-space data.

  Additional density compensation and intensity correction steps are applied
  depending on the input arguments.

  This operator supports batched inputs. All batch shapes should be
  broadcastable with each other.

  This operator supports multicoil imaging. Coil combination is triggered
  when `sensitivities` is not `None`. If you have multiple coils but wish to
  reconstruct each coil separately, simply set `sensitivities` to `None`. The
  coil dimension will then be treated as a standard batch dimension (i.e., it
  becomes part of `...`).

  Args:
    kspace: A `tf.Tensor`. The *k*-space samples. Must have type `complex64` or
      `complex128`. `kspace` can be either Cartesian or non-Cartesian. A
      Cartesian `kspace` must have shape
      `[..., num_coils, *image_shape]`, where `...` are batch dimensions. A
      non-Cartesian `kspace` must have shape `[..., num_coils, num_samples]`.
      If not multicoil (`sensitivities` is `None`), then the `num_coils` axis
      must be omitted.
    image_shape: A 1D integer `tf.Tensor`. Must have length 2 or 3.
      The shape of the reconstructed image[s].
    mask: An optional `tf.Tensor` of type `bool`. The sampling mask. Must have
      shape `[..., *image_shape]`. `mask` should be passed for reconstruction
      from undersampled Cartesian *k*-space. For each point, `mask` should be
      `True` if the corresponding *k*-space sample was measured and `False`
      otherwise.
    trajectory: An optional `tf.Tensor` of type `float32` or `float64`. Must
      have shape `[..., num_samples, rank]`. `trajectory` should be passed for
      reconstruction from non-Cartesian *k*-space.
    density: An optional `tf.Tensor` of type `float32` or `float64`. The
      sampling densities. Must have shape `[..., num_samples]`. This input is
      only relevant for non-Cartesian MRI reconstruction. If passed, the MRI
      linear operator will include sampling density compensation. If `None`,
      the MRI operator will not perform sampling density compensation.
    sensitivities: An optional `tf.Tensor` of type `complex64` or `complex128`.
      The coil sensitivity maps. Must have shape
      `[..., num_coils, *image_shape]`. If provided, a multi-coil parallel
      imaging reconstruction will be performed.
    phase: An optional `tf.Tensor` of type `float32` or `float64`. Must have
      shape `[..., *image_shape]`. A phase estimate for the reconstructed image.
      If provided, a phase-constrained reconstruction will be performed. This
      improves the conditioning of the reconstruction problem in applications
      where there is no interest in the phase data. However, artefacts may
      appear if an inaccurate phase estimate is passed.
    sens_norm: A `boolean`. Whether to normalize coil sensitivities.
      Defaults to `True`.

  Returns:
    A `tf.Tensor`. The reconstructed image. Has the same type as `kspace` and
    shape `[..., *image_shape]`, where `...` is the broadcasted batch shape of
    all inputs.

  Notes:
    Reconstructs an image by applying the adjoint MRI operator to the *k*-space
    data. This typically involves an inverse FFT or a (density-compensated)
    NUFFT, and coil combination for multicoil inputs. This type of
    reconstruction is often called zero-filled reconstruction, because missing
    *k*-space samples are assumed to be zero. Therefore, the resulting image is
    likely to display aliasing artefacts if *k*-space is not sufficiently
    sampled according to the Nyquist criterion.
  """
  # Create the linear operator.
  operator = linear_operator_mri.LinearOperatorMRI(image_shape,
                                                   mask=mask,
                                                   trajectory=trajectory,
                                                   density=density,
                                                   sensitivities=sensitivities,
                                                   phase=phase,
                                                   fft_norm='ortho',
                                                   sens_norm=sens_norm)
  return adjoint(kspace, operator)
