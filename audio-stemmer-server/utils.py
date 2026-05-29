from pathlib import Path

import torch
import torchaudio
from demucs.apply import apply_model
from demucs.pretrained import get_model


class Audio:
    def __init__(self, name: str, output_folder: str | Path | None = None):
        self.audio_path = Path(name).expanduser().resolve()
        self.output_path = (
            Path(output_folder).expanduser().resolve()
            if output_folder is not None
            else self.audio_path.parent
        )

    def get_stems(self, model_name="htdemucs", device="cpu", save=False):
        model = get_model(model_name).to(device)

        wav, sample_rate = torchaudio.load(str(self.audio_path))

        # Demucs is trained on stereo input, so duplicate mono uploads.
        if wav.shape[0] == 1:
            wav = wav.repeat(2, 1)

        wav = wav.to(device)
        sources = apply_model(model, wav[None], device=device)
        stem_names = model.sources

        rendered_paths: dict[str, str] = {}
        if save:
            self.output_path.mkdir(parents=True, exist_ok=True)
            for index, name in enumerate(stem_names):
                stem = sources[0, index].cpu()
                stem_path = self.output_path / f"{self.audio_path.name}_{name}.wav"

                torchaudio.save(str(stem_path), stem, sample_rate)
                rendered_paths[name] = str(stem_path)
                print(f"Saved {name}.wav to {stem_path}")

        return rendered_paths if save else (sources, stem_names, sample_rate)

    @staticmethod
    def extract_stem(sources, stem_names, stem_name):
        for index, stem in enumerate(stem_names):
            if stem == stem_name:
                return sources[0, index]

        return None


def get_default_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"
