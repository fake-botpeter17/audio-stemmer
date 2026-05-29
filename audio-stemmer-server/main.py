import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

from utils import Audio, get_default_device

app = Flask(__name__)
CORS(app)

STEM_NAMES = ("vocals", "drums", "bass", "other")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def set_job(job_id: str, **updates: Any) -> None:
    """Safely update a job record from request and worker threads."""
    with JOBS_LOCK:
        current_job = JOBS.setdefault(job_id, {})
        current_job.update(updates)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return a shallow copy of a job record so callers cannot mutate it."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job is not None else None


def build_stem_urls(job_id: str) -> dict[str, str]:
    return {stem: f"/get-stem/{job_id}/{stem}" for stem in STEM_NAMES}


def process_stems(job_id: str, audio_path: str) -> None:
    """Render stems in a background thread to keep the upload request short."""
    set_job(job_id, status="processing", error=None)

    try:
        audio = Audio(audio_path)
        rendered_stems = audio.get_stems(device=get_default_device(), save=True)
        set_job(
            job_id,
            status="complete",
            stems=rendered_stems,
            stem_urls=build_stem_urls(job_id),
        )
    except Exception as exc:  # noqa: BLE001 - surface worker failures through job status.
        set_job(job_id, status="error", error=str(exc))


@app.route("/stem/<job_id>", methods=["POST"])
def stem_audio(job_id: str):
    audio = request.files.get("audio")
    if audio is None or audio.filename == "":
        return jsonify(
            {"error": "Upload an audio file using the 'audio' form field."}
        ), 400

    suffix = Path(secure_filename(audio.filename)).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as file_handle:
        audio.save(file_handle.name)
        audio_path = file_handle.name

    set_job(job_id, status="queued", audio_path=audio_path, error=None, stems={})
    worker = threading.Thread(
        target=process_stems, args=(job_id, audio_path), daemon=True
    )
    worker.start()

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/get-job-status/<job_id>")
def job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404

    response = {
        "job_id": job_id,
        "status": job.get("status", "unknown"),
        "complete": job.get("status") == "complete",
    }

    if job.get("error"):
        response["error"] = job["error"]

    if job.get("status") == "complete":
        response["stems"] = job.get("stem_urls", build_stem_urls(job_id))

    return jsonify(response)


@app.route("/get-stem/<job_id>/<stem_name>")
def get_stem(job_id: str, stem_name: str):
    if stem_name not in STEM_NAMES:
        return jsonify({"error": "Unknown stem name."}), 404

    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404

    if job.get("status") != "complete":
        return jsonify({"error": "Stem files are not ready yet."}), 409

    stem_path = job.get("stems", {}).get(stem_name)
    if not stem_path or not Path(stem_path).exists():
        return jsonify({"error": "Stem file not found."}), 404

    return send_file(stem_path, mimetype="audio/wav", download_name=f"{stem_name}.wav")


@app.route("/get-stems/<job_id>")
def get_stems(job_id: str):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404

    if job.get("status") != "complete":
        return jsonify({"error": "Stem files are not ready yet."}), 409

    zip_path = Path(tempfile.gettempdir()) / f"{job_id}_stems.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for stem_name, stem_path in job.get("stems", {}).items():
            stem_file = Path(stem_path)
            if stem_file.exists():
                archive.write(stem_file, arcname=f"{stem_name}.wav")

    return send_file(zip_path, as_attachment=True, download_name="stems.zip")


if __name__ == "__main__":
    app.run("0.0.0.0", debug=True, port=8001)
