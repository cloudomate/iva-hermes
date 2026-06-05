#!/bin/bash
# Train a microWakeWord model from a prepared training_parameters_*.yaml.
# Usage: ./train.sh [training_parameters_iva.yaml]
# Run from the micro-wake-word checkout (see TRAINING.md for setup + patches).
set -euo pipefail
CONFIG="${1:-training_parameters_iva.yaml}"
MWW="${MWW_DIR:-$HOME/coderepo/micro-wake-word}"
cd "$MWW"
.venv/bin/python -m microwakeword.model_train_eval \
  --training_config="$CONFIG" \
  --train 1 --restore_checkpoint 1 \
  --test_tf_nonstreaming 0 --test_tflite_nonstreaming 0 \
  --test_tflite_nonstreaming_quantized 0 --test_tflite_streaming 0 \
  --test_tflite_streaming_quantized 1 \
  --use_weights "best_weights" \
  mixednet \
  --pointwise_filters "64,64,64,64" --repeat_in_block "1, 1, 1, 1" \
  --mixconv_kernel_sizes '[5], [7,11], [9,15], [23]' \
  --residual_connection "0,0,0,0" \
  --first_conv_filters 32 --first_conv_kernel_size 5 --stride 3
