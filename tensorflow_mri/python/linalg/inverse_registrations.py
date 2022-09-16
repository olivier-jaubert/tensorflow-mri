# Copyright 2021 The TensorFlow MRI Authors. All Rights Reserved.
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
"""Registrations for LinearOperator.inverse."""

from tensorflow_mri.python.linalg import linear_operator_algebra
from tensorflow_mri.python.linalg import linear_operator_fft


@linear_operator_algebra.RegisterInverse(
    linear_operator_fft.LinearOperatorFFT)
def _inverse_fft(linop):
  if linop.mask is not None:
    raise ValueError("cannot invert masked FFT operator: singular matrix")
  return linop.adjoint()
