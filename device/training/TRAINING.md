# Training custom wake words for the iva voice loop

The headless voice daemon (`../hermes_voice_wake.py`) loads every `*.json`
manifest it finds in `WAKE_MODELS_DIR` (`/home/iva/wakewords` on the device,
mirrored under `../wakewords/` in this repo) via
[`pymicro_wakeword`](https://github.com/OHF-Voice/pymicro-wakeword). Each
manifest points at a quantized `.tflite` produced by
[OHF-Voice/micro-wake-word](https://github.com/OHF-Voice/micro-wake-word).

Models trained locally (all in `../wakewords/`). **Okay Iva** is the active one
on iva; the others are kept for reference / fallback.

| Wake word  | tflite          | manifest        | notes |
|------------|-----------------|-----------------|-------|
| **Okay Iva** (active) | `okay_iva.tflite` | `okay_iva.json` | **real-voice model** — fires ~1.0 on speaker @ cutoff 0.7 |
| Hey Hermes | `hey_hermes.tflite` | `hey_hermes.json` | synthetic only; strong for Piper-like voices |
| Hey Iva    | `hey_iva.tflite`    | `hey_iva.json`    | weak — "iva" too short (2 syll) |

**Wake-word design lessons (empirical):**

1. **Syllable count dominates accuracy.** 2-syllable "Hey Iva" was confusable
   and false-fired; adding a syllable + a hard plosive onset ("**Okay** Iva")
   shifted the whole ROC left. Aim for 3–4 syllables, distinct consonants, and
   a phrase rare in normal speech.

2. **Synthetic Piper audio must match the speaker's pronunciation — and often
   doesn't.** Piper is spelling-driven: it voiced "iva" nothing like the
   speaker's "ee-va". Diagnose with the on-device `WAKE_DEBUG=1` env (logs
   `peak prob_mean`): if real speech scores ~0.0 while the model scores ~1.0 on
   its own synthetic clips (see below), it's a **voice mismatch**, not a cutoff
   problem — lowering `WAKE_CUTOFF` won't help. First try a phonetic respelling
   (`"okay eeva"`); if that still scores ~0, the speaker's voice/accent/mic is
   simply too far from Piper → **train on real recordings** (next section).

3. **Real-voice recipe (what finally worked for Okay Iva).** Record the speaker
   through the *actual device mic + FL channel* with `record_wakeword.py`
   (~40 clips), then mix those (heavily augmented, high `sampling_weight`) with
   the synthetic set and retrain. `prep_real_okayiva.py` +
   `training_parameters_real_okayiva.yaml` are the templates. Result: speaker
   utterances jumped from ~0.0 to ~1.0.

   ```bash
   # on the device (stop the daemon to free the mic):
   systemctl --user stop hermes-voice
   python record_wakeword.py --out /tmp/ww_rec --count 40 --phrase "okay eeva"
   scp 'iva:/tmp/ww_rec/*.wav' ~/coderepo/micro-wake-word/real_samples_okayiva/
   # on the Mac: build features (real high-weight + synthetic) and train
   python prep_real_okayiva.py && ./train.sh training_parameters_real_okayiva.yaml
   ```

   Sanity-check offline before a live test — score the speaker's own clips
   through the deployed model; they should fire ~40/40 at cutoff 0.5.

The daemon applies one shared `WAKE_CUTOFF` (service override, **0.8** for
Okay Iva). To run a **single** wake word, keep only its manifest in
`wakewords/` on the device (others under `wakewords/disabled/`) and
`systemctl --user restart hermes-voice`.

## One-time setup (on the Mac)

```bash
git clone https://github.com/OHF-Voice/micro-wake-word ~/coderepo/micro-wake-word
cd ~/coderepo/micro-wake-word
python3 -m venv .venv && .venv/bin/pip install -e . tensorflow tensorboard
# Sample generator (Piper TTS) for synthetic positives:
git clone https://github.com/rhasspy/piper-sample-generator piper-sample-generator
# download en_US-libritts_r-medium.pt into piper-sample-generator/models/
```

### Required patches (numpy 2.x / torch 2.x drift)

The upstream framework predates the installed numpy/torch; apply these or
training crashes partway through:

- `piper-sample-generator/generate_samples.py` — `torch.load(model_path)` →
  `torch.load(model_path, weights_only=False)`
- `microwakeword/train.py` — `result["fp"].numpy()` → `np.asarray(result["fp"])`
  (and the two sibling `.numpy()` calls); `np.trapz` → `np.trapezoid`
- `microwakeword/test.py` — `np.trapz` → `np.trapezoid`

## Pipeline (per wake word)

```bash
cd ~/coderepo/micro-wake-word

# 1. Generate ~1000 synthetic positives with Piper
.venv/bin/python piper-sample-generator/generate_samples.py "hey iva" \
  --max-samples 1000 --batch-size 100 \
  --model piper-sample-generator/models/en_US-libritts_r-medium.pt \
  --output-dir generated_samples_iva

# 2. Augment + build spectrogram features + write the training config.
#    Reuses mit_rirs/ (room impulse responses), fma_16k/ (music backgrounds),
#    and negative_datasets/ (speech, dinner_party, no_speech) downloaded once
#    for the first model. See prep_wakeword.py for the exact knobs.
.venv/bin/python prep_wakeword.py     # edit input/output dirs per word

# 3. Train + export the quantized streaming tflite (~3-5 min on M-series)
./train.sh training_parameters_iva.yaml
# -> trained_models/wakeword_iva/tflite_stream_state_internal_quant/
#       stream_state_internal_quant.tflite
```

`prep_wakeword.py` (the `prep_iva.py` used for Hey Iva) and the two
`training_parameters_*.yaml` configs in this directory are the reusable
templates — copy and repoint `input_directory` / `features_dir` / `train_dir`
for a new phrase. The negative datasets come from
`kahrendt/microwakeword` on HuggingFace (4 zips); `mit_rirs` from the MIT
Acoustical Reverberation dataset; `fma_16k` is FMA-small resampled to 16 kHz.

## Deploy

```bash
WW=trained_models/wakeword_iva/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite
scp "$WW" iva:/home/iva/wakewords/hey_iva.tflite
scp deploy/iva-hermes-voice/wakewords/hey_iva.json iva:/home/iva/wakewords/
ssh iva 'systemctl --user restart hermes-voice'
# confirm: grep "wake words=" /home/iva/voicewake.log
```

## Picking the cutoff

After training, `tflite_streaming_roc.txt` lists `cutoff : frr : faph`
(false-reject-rate : false-accepts-per-hour). Choose the cutoff where `faph`
is acceptable (~1/hr) while `frr` stays low. Hey Hermes hits frr≈0.04 at 0.7;
Hey Iva needs ~0.6–0.7 for frr≈0.06–0.08. The manifest's `probability_cutoff`
is a default; the daemon's `WAKE_CUTOFF` env overrides it at runtime.
