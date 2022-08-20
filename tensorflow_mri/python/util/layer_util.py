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
"""Layer utilities."""

import tensorflow as tf

from tensorflow_mri.python.layers import coil_compression
from tensorflow_mri.python.layers import coil_sensitivities
from tensorflow_mri.python.layers import convolutional
from tensorflow_mri.python.layers import padding
from tensorflow_mri.python.layers import pooling
from tensorflow_mri.python.layers import reshaping
from tensorflow_mri.python.layers import signal_layers


def get_nd_layer(name, rank):
  """Get an N-D layer object.

  Args:
    name: A `str`. The name of the requested layer.
    rank: An `int`. The rank of the requested layer.

  Returns:
    A `tf.keras.layers.Layer` object.

  Raises:
    ValueError: If the requested layer is unknown to TFMRI.
  """
  try:
    return _ND_LAYERS[(name, rank)]
  except KeyError as err:
    raise ValueError(
        f"Could not find a layer with name '{name}' and rank {rank}.") from err


_ND_LAYERS = {
    ('AveragePooling', 1): pooling.AveragePooling1D,
    ('AveragePooling', 2): pooling.AveragePooling2D,
    ('AveragePooling', 3): pooling.AveragePooling3D,
    ('CoilCompression', 2): coil_compression.CoilCompression2D,
    ('CoilCompression', 3): coil_compression.CoilCompression3D,
    ('CoilSensitivityEstimation', 2): coil_sensitivities.CoilSensitivityEstimation2D,
    ('CoilSensitivityEstimation', 3): coil_sensitivities.CoilSensitivityEstimation3D,
    ('Conv', 1): convolutional.Conv1D,
    ('Conv', 2): convolutional.Conv2D,
    ('Conv', 3): convolutional.Conv3D,
    ('ConvLSTM', 1): tf.keras.layers.ConvLSTM1D,
    ('ConvLSTM', 2): tf.keras.layers.ConvLSTM2D,
    ('ConvLSTM', 3): tf.keras.layers.ConvLSTM3D,
    ('ConvTranspose', 1): tf.keras.layers.Conv1DTranspose,
    ('ConvTranspose', 2): tf.keras.layers.Conv2DTranspose,
    ('ConvTranspose', 3): tf.keras.layers.Conv3DTranspose,
    ('Cropping', 1): tf.keras.layers.Cropping1D,
    ('Cropping', 2): tf.keras.layers.Cropping2D,
    ('Cropping', 3): tf.keras.layers.Cropping3D,
    ('DepthwiseConv', 1): tf.keras.layers.DepthwiseConv1D,
    ('DepthwiseConv', 2): tf.keras.layers.DepthwiseConv2D,
    ('DivisorPadding', 1): padding.DivisorPadding1D,
    ('DivisorPadding', 2): padding.DivisorPadding2D,
    ('DivisorPadding', 3): padding.DivisorPadding3D,
    ('DWT', 1): signal_layers.DWT1D,
    ('DWT', 2): signal_layers.DWT2D,
    ('DWT', 3): signal_layers.DWT3D,
    ('GlobalAveragePooling', 1): tf.keras.layers.GlobalAveragePooling1D,
    ('GlobalAveragePooling', 2): tf.keras.layers.GlobalAveragePooling2D,
    ('GlobalAveragePooling', 3): tf.keras.layers.GlobalAveragePooling3D,
    ('GlobalMaxPool', 1): tf.keras.layers.GlobalMaxPool1D,
    ('GlobalMaxPool', 2): tf.keras.layers.GlobalMaxPool2D,
    ('GlobalMaxPool', 3): tf.keras.layers.GlobalMaxPool3D,
    ('IDWT', 1): signal_layers.IDWT1D,
    ('IDWT', 2): signal_layers.IDWT2D,
    ('IDWT', 3): signal_layers.IDWT3D,
    ('LocallyConnected', 1): tf.keras.layers.LocallyConnected1D,
    ('LocallyConnected', 2): tf.keras.layers.LocallyConnected2D,
    ('MaxPool', 1): pooling.MaxPooling1D,
    ('MaxPool', 2): pooling.MaxPooling2D,
    ('MaxPool', 3): pooling.MaxPooling3D,
    ('SeparableConv', 1): tf.keras.layers.SeparableConv1D,
    ('SeparableConv', 2): tf.keras.layers.SeparableConv2D,
    ('SpatialDropout', 1): tf.keras.layers.SpatialDropout1D,
    ('SpatialDropout', 2): tf.keras.layers.SpatialDropout2D,
    ('SpatialDropout', 3): tf.keras.layers.SpatialDropout3D,
    ('UpSampling', 1): reshaping.UpSampling1D,
    ('UpSampling', 2): reshaping.UpSampling2D,
    ('UpSampling', 3): reshaping.UpSampling3D,
    ('ZeroPadding', 1): tf.keras.layers.ZeroPadding1D,
    ('ZeroPadding', 2): tf.keras.layers.ZeroPadding2D,
    ('ZeroPadding', 3): tf.keras.layers.ZeroPadding3D
}
