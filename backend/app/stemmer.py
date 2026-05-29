from collections.abc import Iterator
from os import getenv
from pathlib import Path
import subprocess
import tempfile
from typing import Any
from uuid import uuid4

import requests
from dotenv import load_dotenv
from flask import Blueprint, Response, jsonify, request, stream_with_context
from requests import Response as RequestsResponse
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

load_dotenv()

PRIVATE_API_URL = (getenv("PRIVATE_API") or "").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 120
STREAM_CHUNK_SIZE = 1024 * 64
MP3_CONTENT_TYPE = "audio/mpeg"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

stemmer_bp = Blueprint("stemmer_bp", __name__)


def private_api_url(path: str) -> str | None:
    """Build a private API URL, or return None when the service is not configured."""
    if not PRIVATE_API_URL:
        return None

    return f"{PRIVATE_API_URL}/{path.lstrip('/')}"


def private_api_not_configured() -> tuple[Response, int]:
    return jsonify({"error": "Private stemmer API is not configured."}), 503


def proxy_headers(response: RequestsResponse) -> dict[str, str]:
    """Copy end-to-end headers from the private API response."""
    return {
        header: value
        for header, value in response.headers.items()
        if header.lower() not in HOP_BY_HOP_HEADERS
    }


def proxy_response(response: RequestsResponse) -> Response:
    return Response(
        response.content,
        status=response.status_code,
        headers=proxy_headers(response),
        content_type=response.headers.get("content-type"),
    )


def stream_proxy_response(response: RequestsResponse) -> Response:
    def generate() -> Iterator[bytes]:
        with response:
            for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if chunk:
                    yield chunk

    return Response(
        stream_with_context(generate()),
        status=response.status_code,
        headers=proxy_headers(response),
        content_type=response.headers.get("content-type"),
    )


def proxy_request_error(error: requests.RequestException) -> tuple[Response, int]:
    return jsonify({"error": f"Unable to reach private stemmer API: {error}"}), 502


def conversion_error(message: str) -> tuple[Response, int]:
    return jsonify({"error": message}), 422


def upload_suffix(audio: FileStorage) -> str:
    """Return a safe suffix for the uploaded source audio temp file."""
    filename = secure_filename(audio.filename or "upload")
    return Path(filename).suffix or ".audio"


def convert_upload_to_mp3(audio: FileStorage) -> Path:
    """Transcode an uploaded audio file to an MP3 temp file for the private API."""
    source_file = tempfile.NamedTemporaryFile(suffix=upload_suffix(audio), delete=False)
    mp3_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    source_path = Path(source_file.name)
    mp3_path = Path(mp3_file.name)
    source_file.close()
    mp3_file.close()

    converted = False
    try:
        audio.save(source_path)
        subprocess.run(
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
                str(mp3_path),
            ],
            check=True,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        converted = True
    except FileNotFoundError as error:
        msg = "ffmpeg is required to convert uploads to MP3 before stemming."
        raise RuntimeError(msg) from error
    except subprocess.TimeoutExpired as error:
        msg = "Timed out while converting the upload to MP3."
        raise RuntimeError(msg) from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode("utf-8", errors="replace").strip()
        details = (
            stderr.splitlines()[-1] if stderr else "ffmpeg could not read the file."
        )
        msg = f"Unable to convert the upload to MP3: {details}"
        raise RuntimeError(msg) from error
    finally:
        source_path.unlink(missing_ok=True)
        if not converted:
            mp3_path.unlink(missing_ok=True)

    return mp3_path


def normalize_stem_urls(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure completed job payloads point clients back at the public API."""
    stems = payload.get("stems")
    if isinstance(stems, dict):
        payload["stems"] = {
            stem_name: f"/get-stem/{job_id}/{stem_name}" for stem_name in stems
        }

    return payload


def forward_stem_request(job_id: str) -> Response | tuple[Response, int]:
    audio = request.files.get("audio")
    if audio is None or audio.filename == "":
        return jsonify(
            {"error": "Upload an audio file using the 'audio' form field."}
        ), 400

    url = private_api_url(f"/stem/{job_id}")
    if url is None:
        return private_api_not_configured()

    converted_path: Path | None = None
    try:
        converted_path = convert_upload_to_mp3(audio)
        with converted_path.open("rb") as mp3_stream:
            files = {"audio": (f"{job_id}.mp3", mp3_stream, MP3_CONTENT_TYPE)}
            response = requests.post(url, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
    except RuntimeError as error:
        return conversion_error(str(error))
    except requests.RequestException as error:
        return proxy_request_error(error)
    finally:
        if converted_path is not None:
            converted_path.unlink(missing_ok=True)

    return proxy_response(response)


@stemmer_bp.route("/stem", methods=["POST"])
def stem_audio_with_generated_job_id():
    return forward_stem_request(str(uuid4()))


@stemmer_bp.route("/stem/<job_id>", methods=["POST"])
def stem_audio(job_id: str):
    return forward_stem_request(job_id)


@stemmer_bp.route("/get-job-status/<job_id>")
def get_job_status(job_id: str):
    url = private_api_url(f"/get-job-status/{job_id}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        return proxy_request_error(error)

    if not response.ok:
        return proxy_response(response)

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return proxy_response(response)

    payload = normalize_stem_urls(job_id, response.json())
    return jsonify(payload), response.status_code


@stemmer_bp.route("/check-status/<job_id>")
def check_job_status(job_id: str):
    return get_job_status(job_id)


@stemmer_bp.route("/get-stem/<job_id>/<stem_name>")
def get_stem(job_id: str, stem_name: str):
    url = private_api_url(f"/get-stem/{job_id}/{stem_name}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        return proxy_request_error(error)

    return stream_proxy_response(response)


@stemmer_bp.route("/get-stems/<job_id>")
def get_stems(job_id: str):
    url = private_api_url(f"/get-stems/{job_id}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        return proxy_request_error(error)

    return stream_proxy_response(response)
