/*Copyright 2021 University College London. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "tensorflow/core/framework/op_kernel.h"

#include "third_party/spiral_waveform/include/spiral_waveform.h"


using namespace tensorflow;

class SpiralWaveformOp : public OpKernel {

 public:

  explicit SpiralWaveformOp(OpKernelConstruction* ctx) : OpKernel(ctx) {

    OP_REQUIRES_OK(ctx, ctx->GetAttr("base_resolution", &base_resolution_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("spiral_arms", &spiral_arms_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("field_of_view", &field_of_view_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("max_grad_ampl", &max_grad_ampl_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("min_rise_time", &min_rise_time_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("dwell_time", &dwell_time_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("readout_os", &readout_os_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("gradient_delay", &gradient_delay_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("larmor_const", &larmor_const_));

  }

  void Compute(OpKernelContext* ctx) override {
                  
    // Create a buffer tensor.
    TensorShape temp_waveform_shape({SWF_MAX_WAVEFORM_SIZE, 2});
    Tensor temp_waveform;
    OP_REQUIRES_OK(ctx, ctx->allocate_temp(DT_FLOAT,
                                           temp_waveform_shape,
                                           &temp_waveform));

    // Calculate the spiral waveform.
    long waveform_length = 0;
    int result = calculate_spiral_trajectory((float*) temp_waveform.data(),
                                             &waveform_length,
                                             (long) base_resolution_,
                                             (long) spiral_arms_,
                                             (double) field_of_view_,
                                             (double) max_grad_ampl_,
                                             (double) min_rise_time_,
                                             (double) dwell_time_,
                                             (double) readout_os_,
                                             (double) gradient_delay_,
                                             (double) larmor_const_);

    OP_REQUIRES(
      ctx, result == 0,
      errors::Internal(
        "failed during `calculate_spiral_trajectory`"));
    
    Tensor waveform = temp_waveform.Slice(0, waveform_length);
    ctx->set_output(0, waveform);
  }

 private:

  int base_resolution_;
  int spiral_arms_;
  float field_of_view_;
  float max_grad_ampl_;
  float min_rise_time_;
  float dwell_time_;
  float readout_os_;
  float gradient_delay_;
  float larmor_const_;

};

REGISTER_KERNEL_BUILDER(
  Name("SpiralWaveform").Device(DEVICE_CPU), SpiralWaveformOp);
