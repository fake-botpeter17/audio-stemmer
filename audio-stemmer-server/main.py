import logging
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

from utils import Audio, get_default_device

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("audio_stemmer.private")

app = Flask(__name__)
CORS(app)

STEM_NAMES = ("vocals", "drums", "bass", "other")
FFMPEG_TIMEOUT_SECONDS = 120
MP3_CONTENT_TYPE = "audio/mpeg"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def file_size(path: str | Path) -> int | None:
    """Return a file size in bytes when the file exists and can be read."""
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def is_mp3_path(path: str | Path) -> bool:
    """Return whether the path points to an MP3 file generated for download."""
    return Path(path).suffix.lower() == ".mp3"


def mp3_output_path(source_path: Path) -> Path:
    """Return an MP3 output path that never overwrites the source file."""
    mp3_path = source_path.with_suffix(".mp3")
    if mp3_path == source_path:
        return source_path.with_name(f"{source_path.stem}-converted.mp3")

    return mp3_path


def ffmpeg_error_details(error: subprocess.CalledProcessError) -> str:
    """Extract the most useful ffmpeg error line for API responses."""
    stderr = error.stderr.decode("utf-8", errors="replace").strip()
    return stderr.splitlines()[-1] if stderr else "ffmpeg could not read the file."


def convert_to_mp3(source: str | Path, mp3_path: str | Path | None = None) -> str:
    """Convert an audio file to MP3 and return the converted file path."""
    source_path = Path(source)
    output_path = (
        Path(mp3_path) if mp3_path is not None else mp3_output_path(source_path)
    )
    if output_path == source_path:
        msg = "MP3 output path must be different from the source path."
        raise RuntimeError(msg)

    logger.info(
        "Converting audio to MP3: source=%s source_size=%s output=%s",
        source_path,
        file_size(source_path),
        output_path,
    )
    converted = False
    try:
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
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        converted = True
        logger.info(
            "MP3 conversion complete: source=%s output=%s output_size=%s",
            source_path,
            output_path,
            file_size(output_path),
        )
    except FileNotFoundError as error:
        logger.exception("ffmpeg was not found while converting source=%s", source_path)
        msg = "ffmpeg is required to convert audio to MP3."
        raise RuntimeError(msg) from error
    except subprocess.TimeoutExpired as error:
        logger.exception(
            "Timed out converting source=%s to output=%s", source_path, output_path
        )
        msg = f"Timed out while converting {source_path.name} to MP3."
        raise RuntimeError(msg) from error
    except subprocess.CalledProcessError as error:
        details = ffmpeg_error_details(error)
        logger.exception(
            "ffmpeg failed converting source=%s to output=%s details=%s",
            source_path,
            output_path,
            details,
        )
        msg = f"Unable to convert {source_path.name} to MP3: {details}"
        raise RuntimeError(msg) from error
    finally:
        if not converted:
            output_path.unlink(missing_ok=True)

    return str(output_path)


def convert_stem_to_mp3(stem_path: str) -> str:
    """Convert a rendered stem file to MP3 before it is exposed to clients."""
    return convert_to_mp3(stem_path)


def convert_stems_to_mp3(rendered_stems: dict[str, str]) -> dict[str, str]:
    """Convert all rendered stems to downloadable MP3 files."""
    downloadable_stems: dict[str, str] = {}
    for stem_name in STEM_NAMES:
        stem_path = rendered_stems.get(stem_name)
        if stem_path is None:
            msg = f"Rendered stem is missing: {stem_name}"
            raise RuntimeError(msg)

        logger.info(
            "Converting rendered stem to MP3: stem=%s path=%s size=%s",
            stem_name,
            stem_path,
            file_size(stem_path),
        )
        converted_path = convert_stem_to_mp3(stem_path)
        if not is_mp3_path(converted_path):
            msg = f"Converted stem is not an MP3 file: {stem_name}"
            raise RuntimeError(msg)

        downloadable_stems[stem_name] = converted_path
        logger.info(
            "Stem ready for download: stem=%s mp3_path=%s size=%s",
            stem_name,
            converted_path,
            file_size(converted_path),
        )

    return downloadable_stems


def set_job(job_id: str, **updates: Any) -> None:
    """Safely update a job record from request and worker threads."""
    logger.info("Updating job: job_id=%s updates=%s", job_id, sorted(updates))
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
    logger.info(
        "Starting stem processing: job_id=%s audio_path=%s size=%s",
        job_id,
        audio_path,
        file_size(audio_path),
    )
    set_job(job_id, status="processing", error=None)

    try:
        logger.info("Loading audio for stem processing: job_id=%s", job_id)
        audio = Audio(audio_path)
        device = get_default_device()
        logger.info("Rendering stems: job_id=%s device=%s", job_id, device)
        rendered_stems = audio.get_stems(device=device, save=True)
        logger.info(
            "Rendered stems: job_id=%s stems=%s",
            job_id,
            {stem: rendered_stems.get(stem) for stem in STEM_NAMES},
        )
        downloadable_stems = convert_stems_to_mp3(rendered_stems)
        set_job(
            job_id,
            status="complete",
            stems=downloadable_stems,
            stem_urls=build_stem_urls(job_id),
        )
        logger.info(
            "Stem processing complete: job_id=%s stems=%s",
            job_id,
            sorted(downloadable_stems),
        )
    except Exception as exc:  # noqa: BLE001 - surface worker failures through job status.
        logger.exception(
            "Stem processing failed: job_id=%s audio_path=%s", job_id, audio_path
        )
        set_job(job_id, status="error", error=str(exc))


@app.route("/stem/<job_id>", methods=["POST"])
def stem_audio(job_id: str):
    logger.info("Received private stem request: job_id=%s", job_id)
    audio = request.files.get("audio")
    if audio is None or audio.filename == "":
        return jsonify(
            {"error": "Upload an audio file using the 'audio' form field."}
        ), 400

    logger.info(
        "Private upload accepted: job_id=%s filename=%s content_type=%s",
        job_id,
        audio.filename,
        audio.content_type,
    )
    suffix = Path(secure_filename(audio.filename or "")).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as file_handle:
        audio.save(file_handle.name)
        audio_path = file_handle.name

    logger.info(
        "Private upload saved: job_id=%s path=%s size=%s",
        job_id,
        audio_path,
        file_size(audio_path),
    )
    set_job(job_id, status="queued", audio_path=audio_path, error=None, stems={})
    worker = threading.Thread(
        target=process_stems, args=(job_id, audio_path), daemon=True
    )
    worker.start()
    logger.info("Stem worker queued: job_id=%s thread=%s", job_id, worker.name)

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/get-job-status/<job_id>")
def job_status(job_id: str):
    logger.info("Private job status requested: job_id=%s", job_id)
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

    logger.info(
        "Private job status response: job_id=%s status=%s complete=%s",
        job_id,
        response["status"],
        response["complete"],
    )
    return jsonify(response)


@app.route("/get-stem/<job_id>/<stem_name>")
def get_stem(job_id: str, stem_name: str):
    logger.info("Private stem download requested: job_id=%s stem=%s", job_id, stem_name)
    if stem_name not in STEM_NAMES:
        return jsonify({"error": "Unknown stem name."}), 404

    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404

    if job.get("status") != "complete":
        return jsonify({"error": "Stem files are not ready yet."}), 409

    stem_path = job.get("stems", {}).get(stem_name)
    if not stem_path or not Path(stem_path).exists():
        logger.warning(
            "Stem file not found: job_id=%s stem=%s path=%s",
            job_id,
            stem_name,
            stem_path,
        )
        return jsonify({"error": "Stem file not found."}), 404
    if not is_mp3_path(stem_path):
        logger.error(
            "Refusing to send non-MP3 stem: job_id=%s stem=%s path=%s",
            job_id,
            stem_name,
            stem_path,
        )
        return jsonify({"error": "Stem file is not available as MP3."}), 500

    logger.info(
        "Sending MP3 stem: job_id=%s stem=%s path=%s size=%s",
        job_id,
        stem_name,
        stem_path,
        file_size(stem_path),
    )
    return send_file(
        stem_path, mimetype=MP3_CONTENT_TYPE, download_name=f"{stem_name}.mp3"
    )


@app.route("/get-stems/<job_id>")
def get_stems(job_id: str):
    logger.info("Private stem archive requested: job_id=%s", job_id)
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404

    if job.get("status") != "complete":
        return jsonify({"error": "Stem files are not ready yet."}), 409

    zip_path = Path(tempfile.gettempdir()) / f"{job_id}_stems.zip"
    archived_stems: list[str] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for stem_name in STEM_NAMES:
            stem_path = job.get("stems", {}).get(stem_name)
            stem_file = Path(stem_path) if stem_path else None
            if stem_file is None or not stem_file.exists():
                logger.warning(
                    "Skipping missing stem in archive: job_id=%s stem=%s path=%s",
                    job_id,
                    stem_name,
                    stem_path,
                )
                continue
            if not is_mp3_path(stem_file):
                logger.error(
                    "Refusing to archive non-MP3 stem: job_id=%s stem=%s path=%s",
                    job_id,
                    stem_name,
                    stem_file,
                )
                continue

            archive.write(stem_file, arcname=f"{stem_name}.mp3")
            archived_stems.append(stem_name)

    logger.info(
        "Sending MP3 stem archive: job_id=%s zip_path=%s size=%s stems=%s",
        job_id,
        zip_path,
        file_size(zip_path),
        archived_stems,
    )
    return send_file(zip_path, as_attachment=True, download_name="stems.zip")


if __name__ == "__main__":
    app.run("0.0.0.0", debug=True, port=8001)
