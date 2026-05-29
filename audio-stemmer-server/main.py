import tempfile
import zipfile
import os
from flask import Flask, request, send_file
from flask_cors import CORS
from utils import Audio

app = Flask(__name__)
CORS(app)

JOBS = {}


@app.route("/stem/<job_id>", methods=["POST"])
def stem_audio(job_id):
    JOBS[job_id] = {"status": False}
    print(JOBS)
    audio = request.files.get("audio")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        audio.save(f.name)

    JOBS[job_id]["name"] = f.name
    a = Audio(f.name)
    a.get_stems(device="cuda", save=True)
    return True


@app.route("/get-stems/<job_id>")
def get_stems(job_id):
    name = JOBS[job_id]["name"]

    zip_path = f"{name}_stems.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for stem in ["vocals", "drums", "bass", "other"]:
            stem_file = f"{name}_{stem}.mp3"

            if os.path.exists(stem_file):
                z.write(stem_file, arcname=f"{stem}.mp3")
    JOBS[job_id]["status"] = True
    print(JOBS)

    return send_file(zip_path, as_attachment=True, download_name="stems.zip")


@app.route("/get-job-status/<job_id>")
def job_status(job_id):
    print(JOBS)
    return JOBS[job_id]["status"]


app.run("0.0.0.0", debug=True, port=8001)
