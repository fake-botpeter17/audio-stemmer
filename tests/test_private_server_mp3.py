from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def private_server_module():
    utils = types.ModuleType("utils")
    utils.Audio = object
    utils.get_default_device = lambda: "cpu"
    sys.modules.setdefault("utils", utils)

    module_path = (
        Path(__file__).resolve().parents[1] / "audio-stemmer-server" / "main.py"
    )
    spec = importlib.util.spec_from_file_location("private_stemmer_main", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mp3_output_path_avoids_source_overwrite(private_server_module, tmp_path):
    source_path = tmp_path / "vocals.mp3"

    output_path = private_server_module.mp3_output_path(source_path)

    assert output_path == tmp_path / "vocals-converted.mp3"


def test_convert_to_mp3_invokes_ffmpeg(private_server_module, tmp_path, monkeypatch):
    source_path = tmp_path / "drums.wav"
    source_path.write_bytes(b"fake audio")
    output_path = tmp_path / "drums.mp3"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output_path.write_bytes(b"fake mp3")

    monkeypatch.setattr(private_server_module.subprocess, "run", fake_run)

    converted_path = private_server_module.convert_to_mp3(source_path)

    assert converted_path == str(output_path)
    assert calls == [
        (
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "320k",
                str(output_path),
            ],
            {
                "check": True,
                "capture_output": True,
                "timeout": private_server_module.FFMPEG_TIMEOUT_SECONDS,
            },
        )
    ]


def test_convert_to_mp3_rejects_same_source_and_output(private_server_module, tmp_path):
    source_path = tmp_path / "bass.mp3"

    with pytest.raises(RuntimeError, match="must be different"):
        private_server_module.convert_to_mp3(source_path, source_path)


def test_convert_stems_to_mp3_keeps_only_expected_mp3_stems(
    private_server_module, tmp_path, monkeypatch
):
    rendered_stems = {}
    for stem_name in (*private_server_module.STEM_NAMES, "piano"):
        stem_path = tmp_path / f"{stem_name}.wav"
        stem_path.write_bytes(b"fake audio")
        rendered_stems[stem_name] = str(stem_path)

    def fake_convert(stem_path):
        mp3_path = Path(stem_path).with_suffix(".mp3")
        mp3_path.write_bytes(b"fake mp3")
        return str(mp3_path)

    monkeypatch.setattr(private_server_module, "convert_stem_to_mp3", fake_convert)

    downloadable_stems = private_server_module.convert_stems_to_mp3(rendered_stems)

    assert set(downloadable_stems) == set(private_server_module.STEM_NAMES)
    assert all(
        Path(stem_path).suffix == ".mp3" for stem_path in downloadable_stems.values()
    )


def test_get_stem_refuses_non_mp3_files(private_server_module, tmp_path):
    private_server_module.JOBS.clear()
    wav_path = tmp_path / "vocals.wav"
    wav_path.write_bytes(b"fake wav")
    private_server_module.set_job(
        "non-mp3-job", status="complete", stems={"vocals": str(wav_path)}
    )

    client = private_server_module.app.test_client()
    response = client.get("/get-stem/non-mp3-job/vocals")

    assert response.status_code == 500
    assert response.get_json() == {"error": "Stem file is not available as MP3."}
