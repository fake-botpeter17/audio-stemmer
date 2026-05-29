from flask import Flask
from flask_cors import CORS

from .stemmer import stemmer_bp


def create_app():
    app = Flask(__name__)
    app.register_blueprint(stemmer_bp)

    CORS(app, supports_credentials=True)
    return app
