from flask import Blueprint, request
from dotenv import load_dotenv
from os import getenv
from requests import post, get
import uuid

load_dotenv()

HOME_PC_URL = getenv("PRIVATE_API")
print(f"{HOME_PC_URL = }")

stemmer_bp = Blueprint("stemmer_bp", __name__)


@stemmer_bp.route("/stem", methods=["POST"])
def stem_audio():
    audio = request.files["audio"]
    job_id = str(uuid.uuid1())

    post(
        f"{HOME_PC_URL}/stem/{job_id}",
        files={"audio": (audio.filename, audio.stream, audio.content_type)},
    )

    return job_id


@stemmer_bp.route("/check-status/<int:job_id>")
def check_job_status(job_id):
    res = get(f"{HOME_PC_URL}/get-job-status/{job_id}")
    print(f"{res = }")
    return res
