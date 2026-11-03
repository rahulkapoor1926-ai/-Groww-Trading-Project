import logging
import os

from dotenv import load_dotenv
from flask import Flask

from .groww_client import GrowwSession

log = logging.getLogger(__name__)


def create_app(
    groww_session: GrowwSession | None = None,
    database_url: str | None = None,
    api_factory=None,
    paper_clock=None,
) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
    app.extensions["groww"] = groww_session or GrowwSession.from_env()

    from . import db
    from .groww_data import ReadOnlyGroww
    from .jobs import JobManager
    from .paper import PaperEngine, ist_now

    engine = db.make_engine(database_url)
    app.extensions["db"] = engine
    app.extensions["db_error"] = None
    try:
        db.init_db(engine)
    except Exception as exc:  # show a friendly error state instead of crashing
        log.warning("Database not reachable: %s", exc)
        app.extensions["db_error"] = (
            "Can't reach the Postgres database. In Codespaces it starts with the "
            f"codespace; try rebuilding the container. ({type(exc).__name__})"
        )

    if api_factory is None:
        wrappers: dict = {}

        def api_factory():
            client = app.extensions["groww"].client()  # logs in with TOTP if needed
            key = id(client)
            if key not in wrappers:
                wrappers.clear()
                wrappers[key] = ReadOnlyGroww(client)
            return wrappers[key]

    app.extensions["groww_api"] = api_factory
    app.extensions["jobs"] = JobManager()
    app.extensions["paper"] = PaperEngine(
        engine, api_factory, clock=paper_clock or ist_now
    )

    from .dashboard import bp as dash_bp
    from .routes import bp

    app.register_blueprint(bp)
    app.register_blueprint(dash_bp)
    return app
