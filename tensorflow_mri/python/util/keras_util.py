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
"""Keras utilities."""

import tensorflow as tf


class LossFunctionWrapper(tf.keras.losses.Loss):
  """Wraps a loss function in the `Loss` class."""

  def __init__(self,
               fn,
               reduction=tf.keras.losses.Reduction.AUTO,
               name=None,
               **kwargs):
    """Initializes `LossFunctionWrapper` class.

    Args:
      fn: The loss function to wrap, with signature `fn(y_true, y_pred,
        **kwargs)`.
      reduction: Type of `tf.keras.losses.Reduction` to apply to
        loss. Default value is `AUTO`. `AUTO` indicates that the reduction
        option will be determined by the usage context. For almost all cases
        this defaults to `SUM_OVER_BATCH_SIZE`. When used with
        `tf.distribute.Strategy`, outside of built-in training loops such as
        `tf.keras` `compile` and `fit`, using `AUTO` or `SUM_OVER_BATCH_SIZE`
        will raise an error. Please see this custom training [tutorial](
          https://www.tensorflow.org/tutorials/distribute/custom_training) for
            more details.
      name: Optional name for the instance.
      **kwargs: The keyword arguments that are passed on to `fn`.
    """
    super().__init__(reduction=reduction, name=name)
    self.fn = fn
    self._fn_kwargs = kwargs

  def call(self, y_true, y_pred):
    """Invokes the `LossFunctionWrapper` instance.

    Args:
      y_true: Ground truth values.
      y_pred: The predicted values.

    Returns:
      Loss values per sample.
    """
    return self.fn(y_true, y_pred, **self._fn_kwargs)

  def get_config(self):
    config = {}
    for k, v in self._fn_kwargs.items():
      config[k] = tf.keras.backend.eval(v) if is_tensor_or_variable(v) else v
    base_config = super().get_config()
    return {**base_config, **config}


def is_tensor_or_variable(x):
  return tf.is_tensor(x) or isinstance(x, tf.Variable)


def complexx():
  """Returns the default complex dtype, as a string.

  The default complex dtype is the complex equivalent of the default
  float type, which can be obtained as `tf.keras.backend.floatx()`.

  To change the default complex dtype, change the default float type via
  `tf.keras.backend.set_floatx()`.

  Returns:
    The current default complex dtype, as a string.
  """
  complex_dtypes = {
      'float32': 'complex64',
      'float64': 'complex128'
  }
  return tf.dtypes.as_dtype(complex_dtypes[tf.keras.backend.floatx()]).name
