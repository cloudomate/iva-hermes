import os
os.chdir("/Users/yashwant.singh/coderepo/micro-wake-word")
from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration
from mmap_ninja.ragged import RaggedMmap
import yaml
print("[iva] augmented features...", flush=True)
clips=Clips(input_directory='generated_samples_iva', file_pattern='*.wav', max_clip_duration_s=None,
            remove_silence=False, random_split_seed=10, split_count=0.1)
augmenter=Augmentation(augmentation_duration_s=3.2,
    augmentation_probabilities={"SevenBandParametricEQ":0.1,"TanhDistortion":0.1,"PitchShift":0.1,
        "BandStopFilter":0.1,"AddColorNoise":0.1,"AddBackgroundNoise":0.75,"Gain":1.0,"RIR":0.5},
    impulse_paths=['mit_rirs'], background_paths=['fma_16k'],
    background_min_snr_db=-5, background_max_snr_db=10, min_jitter_s=0.195, max_jitter_s=0.205)
os.makedirs("generated_augmented_features_iva", exist_ok=True)
for split in ["training","validation","testing"]:
    out=f"generated_augmented_features_iva/{split}"; os.makedirs(out, exist_ok=True)
    if split=="validation": sn,rep,sf="validation",1,10
    elif split=="testing": sn,rep,sf="test",1,1
    else: sn,rep,sf="train",2,10
    spg=SpectrogramGeneration(clips=clips, augmenter=augmenter, slide_frames=sf, step_ms=10)
    RaggedMmap.from_generator(out_dir=os.path.join(out,"wakeword_mmap"),
        sample_generator=spg.spectrogram_generator(split=sn, repeat=rep), batch_size=100, verbose=True)
config={"window_step_ms":10,"train_dir":"trained_models/wakeword_iva","features":[
  {"features_dir":"generated_augmented_features_iva","sampling_weight":2.0,"penalty_weight":1.0,"truth":True,"truncation_strategy":"truncate_start","type":"mmap"},
  {"features_dir":"negative_datasets/speech","sampling_weight":10.0,"penalty_weight":1.0,"truth":False,"truncation_strategy":"random","type":"mmap"},
  {"features_dir":"negative_datasets/dinner_party","sampling_weight":10.0,"penalty_weight":1.0,"truth":False,"truncation_strategy":"random","type":"mmap"},
  {"features_dir":"negative_datasets/no_speech","sampling_weight":5.0,"penalty_weight":1.0,"truth":False,"truncation_strategy":"random","type":"mmap"},
  {"features_dir":"negative_datasets/dinner_party_eval","sampling_weight":0.0,"penalty_weight":1.0,"truth":False,"truncation_strategy":"split","type":"mmap"}],
 "training_steps":[10000],"positive_class_weight":[1],"negative_class_weight":[20],"learning_rates":[0.001],"batch_size":128,
 "time_mask_max_size":[0],"time_mask_count":[0],"freq_mask_max_size":[0],"freq_mask_count":[0],
 "eval_step_interval":500,"clip_duration_ms":1500,"target_minimization":0.9,"minimization_metric":None,"maximization_metric":"average_viable_recall"}
yaml.dump(config, open("training_parameters_iva.yaml","w"))
print("[iva] DONE features+config", flush=True)
