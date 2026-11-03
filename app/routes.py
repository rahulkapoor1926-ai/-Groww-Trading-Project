from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from growwapi.groww.exceptions import GrowwAPIException

from .groww_client import GrowwConfigError, GrowwSession

bp = Blueprint("main", __name__)


def groww() -> GrowwSession:
    return current_app.extensions["groww"]


def _error_text(exc: Exception) -> str:
    msg = getattr(exc, "msg", None) or str(exc) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {msg}"


@bp.get("/")
def index():
    session = groww()
    profile = margins = None
    errors = []
    if session.logged_in:
        client = session.client()
        try:
            profile = client.get_user_profile(timeout=10)
        except Exception as exc:  # show the error on the page, don't crash
            errors.append(f"Profile: {_error_text(exc)}")
        try:
            margins = client.get_available_margin_details(timeout=10)
        except Exception as exc:
            errors.append(f"Funds: {_error_text(exc)}")
    return render_template(
        "index.html",
        session=session,
        profile=profile,
        margins=margins,
        errors=errors,
    )


@bp.post("/login")
def login():
    try:
        groww().login()
        flash("Connected to Groww.", "ok")
    except GrowwConfigError as exc:
        flash(str(exc), "error")
    except GrowwAPIException as exc:
        flash(f"Groww login failed. {_error_text(exc)}", "error")
    except Exception as exc:
        flash(f"Login failed. {_error_text(exc)}", "error")
    return redirect(url_for("main.index"))


@bp.post("/logout")
def logout():
    groww().logout()
    flash("Disconnected.", "ok")
    return redirect(url_for("main.index"))


@bp.get("/health")
def health():
    s = groww()
    return {"status": "ok", "configured": s.configured, "logged_in": s.logged_in}
