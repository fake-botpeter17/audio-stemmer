from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def public_stemmer_module():
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    requests.Response = object
    requests.post = lambda *args, **kwargs: None
    requests.get = lambda *args, **kwargs: None
    sys.modules.setdefault("requests", requests)

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules.setdefault("dotenv", dotenv)

    flask = types.ModuleType("flask")

    class Blueprint:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            return lambda function: function

    flask.Blueprint = Blueprint
    flask.Response = object
    flask.jsonify = lambda payload: payload
    flask.request = types.SimpleNamespace(files={})
    flask.stream_with_context = lambda generator: generator
    sys.modules.setdefault("flask", flask)

    datastructures = types.ModuleType("werkzeug.datastructures")

    class FileStorage:
        def __init__(self, filename: str, content_type: str):
            self.filename = filename
            self.content_type = content_type
            self.stream = io.BytesIO(b"fake media")

        def save(self, path):
            with open(path, "wb") as output:
                output.write(self.stream.getvalue())

    datastructures.FileStorage = FileStorage
    sys.modules.setdefault("werkzeug.datastructures", datastructures)

    utils = types.ModuleType("werkzeug.utils")
    utils.secure_filename = lambda filename: filename.replace("/", "_")
    sys.modules.setdefault("werkzeug.utils", utils)

    module_path = Path(__file__).resolve().parents[1] / "backend" / "app" / "stemmer.py"
    spec = importlib.util.spec_from_file_location("public_stemmer", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_upload(
    public_stemmer_module,
    filename: str,
    content_type: str = "application/octet-stream",
):
    return public_stemmer_module.FileStorage(
        filename=filename,
        content_type=content_type,
    )


def test_upload_suffix_preserves_convertible_media_extensions(public_stemmer_module):
    assert (
        public_stemmer_module.upload_suffix(
            make_upload(public_stemmer_module, "song.m4a", "audio/mp4")
        )
        == ".m4a"
    )
    assert (
        public_stemmer_module.upload_suffix(
            make_upload(public_stemmer_module, "clip.mp4", "video/mp4")
        )
        == ".mp4"
    )


def test_upload_suffix_falls_back_for_extensionless_uploads(public_stemmer_module):
    assert (
        public_stemmer_module.upload_suffix(
            make_upload(public_stemmer_module, "voice-note")
        )
        == ".audio"
    )
