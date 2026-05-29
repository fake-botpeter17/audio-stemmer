from demucs.pretrained import get_model
from demucs.apply import apply_model

import torchaudio
import os.path as path


class Audio:
    def __init__(self, name: str, assets_folder="tmp", output_folder="outputs"):
        self.audio_name = name
        self.assets_path = path.abspath(assets_folder)
        self.output_path = path.abspath(output_folder)

        self.audio_path = path.join(self.assets_path, self.audio_name)

    def get_stems(self, model_name="htdemucs", device="cpu", save=False):
        model = get_model(model_name).to(device)

        wav, sr = torchaudio.load(self.audio_path)

        # mono -> stereo cause model is trained only on
        if wav.shape[0] == 1:
            wav = wav.repeat(2, 1)

        wav = wav.to(device)

        sources = apply_model(model, wav[None], device=device)

        stem_names = model.sources

        if save:
            for i, name in enumerate(stem_names):
                stem = sources[0, i].cpu()

                torchaudio.save(
                    f"{path.join(self.output_path, f'{self.audio_name}_{name}')}.wav",
                    stem.cpu(),
                    sr,
                )

                print(f"Saved {name}.wav")

        return sources, stem_names, sr

    @staticmethod
    def extract_stem(sources, stem_names, stem_name):
        for i, stem in enumerate(stem_names):
            if stem == stem_name:
                return sources[0, i]
