from collections.abc import Iterator
from os import getenv
from typing import Any
from uuid import uuid4

import requests
from dotenv import load_dotenv
from flask import Blueprint, Response, jsonify, request, stream_with_context
from requests import Response as RequestsResponse

load_dotenv()

PRIVATE_API_URL = (getenv("PRIVATE_API") or "").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 30
STREAM_CHUNK_SIZE = 1024 * 64
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

    files = {"audio": (audio.filename, audio.stream, audio.content_type)}
    try:
        response = requests.post(url, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        return proxy_request_error(error)

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
