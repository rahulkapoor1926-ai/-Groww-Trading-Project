import os

from dotenv import load_dotenv
from flask import Flask

from .groww_client import GrowwSession


def create_app(groww_session: GrowwSession | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
    app.extensions["groww"] = groww_session or GrowwSession.from_env()

    from .routes import bp

    app.register_blueprint(bp)
    return app
