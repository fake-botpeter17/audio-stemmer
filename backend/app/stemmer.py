from collections.abc import Iterator
import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("audio_stemmer.public")

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


def file_size(path: str | Path) -> int | None:
    """Return a file size in bytes when the file exists and can be read."""
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def private_api_url(path: str) -> str | None:
    """Build a private API URL, or return None when the service is not configured."""
    if not PRIVATE_API_URL:
        logger.warning("Private API URL is not configured for path=%s", path)
        return None

    url = f"{PRIVATE_API_URL}/{path.lstrip('/')}"
    logger.info("Built private API URL: path=%s url=%s", path, url)
    return url


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
    logger.info(
        "Proxying private response: status=%s content_type=%s bytes=%s",
        response.status_code,
        response.headers.get("content-type"),
        len(response.content),
    )
    return Response(
        response.content,
        status=response.status_code,
        headers=proxy_headers(response),
        content_type=response.headers.get("content-type"),
    )


def stream_proxy_response(response: RequestsResponse) -> Response:
    logger.info(
        "Streaming private response: status=%s content_type=%s",
        response.status_code,
        response.headers.get("content-type"),
    )

    def generate() -> Iterator[bytes]:
        streamed_bytes = 0
        with response:
            for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if chunk:
                    streamed_bytes += len(chunk)
                    yield chunk
        logger.info("Finished streaming private response: bytes=%s", streamed_bytes)

    return Response(
        stream_with_context(generate()),
        status=response.status_code,
        headers=proxy_headers(response),
        content_type=response.headers.get("content-type"),
    )


def proxy_request_error(error: requests.RequestException) -> tuple[Response, int]:
    logger.exception("Unable to reach private stemmer API")
    return jsonify({"error": f"Unable to reach private stemmer API: {error}"}), 502


def conversion_error(message: str) -> tuple[Response, int]:
    logger.warning("Upload conversion failed: %s", message)
    return jsonify({"error": message}), 422


def upload_suffix(audio: FileStorage) -> str:
    """Return a safe suffix for the uploaded source audio temp file."""
    filename = secure_filename(audio.filename or "upload")
    return Path(filename).suffix or ".audio"


def convert_upload_to_mp3(audio: FileStorage) -> Path:
    """Transcode an uploaded audio file to an MP3 temp file for the private API."""
    logger.info(
        "Preparing upload conversion: filename=%s content_type=%s",
        audio.filename,
        audio.content_type,
    )
    source_file = tempfile.NamedTemporaryFile(suffix=upload_suffix(audio), delete=False)
    mp3_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    source_path = Path(source_file.name)
    mp3_path = Path(mp3_file.name)
    source_file.close()
    mp3_file.close()

    converted = False
    try:
        audio.save(source_path)
        logger.info(
            "Saved upload for MP3 conversion: source=%s source_size=%s mp3_output=%s",
            source_path,
            file_size(source_path),
            mp3_path,
        )
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
        logger.info(
            "Upload MP3 conversion complete: mp3_path=%s mp3_size=%s",
            mp3_path,
            file_size(mp3_path),
        )
    except FileNotFoundError as error:
        logger.exception(
            "ffmpeg was not found while converting upload source=%s", source_path
        )
        msg = "ffmpeg is required to convert uploads to MP3 before stemming."
        raise RuntimeError(msg) from error
    except subprocess.TimeoutExpired as error:
        logger.exception(
            "Timed out converting upload source=%s output=%s", source_path, mp3_path
        )
        msg = "Timed out while converting the upload to MP3."
        raise RuntimeError(msg) from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode("utf-8", errors="replace").strip()
        details = (
            stderr.splitlines()[-1] if stderr else "ffmpeg could not read the file."
        )
        logger.exception(
            "ffmpeg failed converting upload source=%s output=%s details=%s",
            source_path,
            mp3_path,
            details,
        )
        msg = f"Unable to convert the upload to MP3: {details}"
        raise RuntimeError(msg) from error
    finally:
        logger.info("Removing temporary upload source: source=%s", source_path)
        source_path.unlink(missing_ok=True)
        if not converted:
            logger.info("Removing failed MP3 output: mp3_path=%s", mp3_path)
            mp3_path.unlink(missing_ok=True)

    return mp3_path


def normalize_stem_urls(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure completed job payloads point clients back at the public API."""
    stems = payload.get("stems")
    if isinstance(stems, dict):
        logger.info(
            "Normalizing stem URLs for public client: job_id=%s stems=%s",
            job_id,
            sorted(stems),
        )
        payload["stems"] = {
            stem_name: f"/get-stem/{job_id}/{stem_name}" for stem_name in stems
        }

    return payload


def forward_stem_request(job_id: str) -> Response | tuple[Response, int]:
    logger.info("Received public stem request: job_id=%s", job_id)
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
        logger.info(
            "Forwarding MP3 upload to private API: job_id=%s url=%s mp3_path=%s mp3_size=%s",
            job_id,
            url,
            converted_path,
            file_size(converted_path),
        )
        with converted_path.open("rb") as mp3_stream:
            files = {"audio": (f"{job_id}.mp3", mp3_stream, MP3_CONTENT_TYPE)}
            response = requests.post(url, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
        logger.info(
            "Private stem request completed: job_id=%s status=%s content_type=%s",
            job_id,
            response.status_code,
            response.headers.get("content-type"),
        )
    except RuntimeError as error:
        return conversion_error(str(error))
    except requests.RequestException as error:
        return proxy_request_error(error)
    finally:
        if converted_path is not None:
            logger.info(
                "Removing forwarded MP3 temp file: job_id=%s mp3_path=%s",
                job_id,
                converted_path,
            )
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
    logger.info("Public job status requested: job_id=%s", job_id)
    url = private_api_url(f"/get-job-status/{job_id}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        logger.info(
            "Private job status completed: job_id=%s status=%s content_type=%s",
            job_id,
            response.status_code,
            response.headers.get("content-type"),
        )
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
    logger.info("Public stem download requested: job_id=%s stem=%s", job_id, stem_name)
    url = private_api_url(f"/get-stem/{job_id}/{stem_name}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS)
        logger.info(
            "Private stem download response: job_id=%s stem=%s status=%s content_type=%s",
            job_id,
            stem_name,
            response.status_code,
            response.headers.get("content-type"),
        )
    except requests.RequestException as error:
        return proxy_request_error(error)

    if response.ok and "audio/mpeg" not in response.headers.get("content-type", ""):
        logger.error(
            "Private API returned a non-MP3 stem response: job_id=%s stem=%s content_type=%s",
            job_id,
            stem_name,
            response.headers.get("content-type"),
        )
        response.close()
        return jsonify({"error": "Private API did not return an MP3 stem."}), 502

    return stream_proxy_response(response)


@stemmer_bp.route("/get-stems/<job_id>")
def get_stems(job_id: str):
    logger.info("Public stem archive requested: job_id=%s", job_id)
    url = private_api_url(f"/get-stems/{job_id}")
    if url is None:
        return private_api_not_configured()

    try:
        response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS)
        logger.info(
            "Private stem archive response: job_id=%s status=%s content_type=%s",
            job_id,
            response.status_code,
            response.headers.get("content-type"),
        )
    except requests.RequestException as error:
        return proxy_request_error(error)

    return stream_proxy_response(response)
