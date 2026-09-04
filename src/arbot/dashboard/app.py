from __future__ import annotations

import os
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from arbot.config import ROOT
from arbot.dashboard.data import (
    fetch_dashboard,
    reset_all_statistics,
    reset_strategy_budgets,
    save_dashboard_settings,
    set_strategy_budget,
)
from arbot.mode import load_dotenv_file

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
PORT = int(os.getenv("PORT", "8788"))


def _check_auth(username: str, password: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    return username == DASHBOARD_USER and password == DASHBOARD_PASSWORD


def _authenticate() -> Response:
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Arbot Dashboard"'},
    )


def requires_auth(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return fn(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _authenticate()
        return fn(*args, **kwargs)

    return decorated


@app.route("/")
@requires_auth
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
@requires_auth
def dashboard_api():
    try:
        payload = fetch_dashboard(
            data_dir=Path(request.args["data_dir"]) if request.args.get("data_dir") else None,
            mode=request.args.get("mode"),
        )
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/settings", methods=["GET", "POST"])
@requires_auth
def settings_api():
    try:
        mode = request.args.get("mode")
        data_dir = request.args.get("data_dir")
        if request.method == "GET":
            payload = fetch_dashboard(
                data_dir=Path(data_dir) if data_dir else None,
                mode=mode,
            )
            return jsonify({"ok": True, "settings": payload.get("settings")})
        body = request.get_json(silent=True) or {}
        updates = body.get("settings") if isinstance(body.get("settings"), dict) else body
        payload = save_dashboard_settings(
            updates,
            data_dir=Path(data_dir or body.get("data_dir") or "") if (data_dir or body.get("data_dir")) else None,
            mode=mode or body.get("mode"),
        )
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset-balances", methods=["POST"])
@requires_auth
def reset_balances_api():
    try:
        body = request.get_json(silent=True) or {}
        balance = body.get("balance")
        if balance is None:
            return jsonify({"ok": False, "error": "balance is required"}), 400
        payload = reset_strategy_budgets(
            data_dir=Path(body["data_dir"]) if body.get("data_dir") else None,
            mode=request.args.get("mode") or body.get("mode"),
            balance=float(balance),
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset-statistics", methods=["POST"])
@requires_auth
def reset_statistics_api():
    try:
        body = request.get_json(silent=True) or {}
        payload = reset_all_statistics(
            data_dir=Path(body["data_dir"]) if body.get("data_dir") else None,
            mode=request.args.get("mode") or body.get("mode"),
            balance=float(body["balance"]) if body.get("balance") is not None else None,
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/set-strategy-budget", methods=["POST"])
@requires_auth
def set_strategy_budget_api():
    try:
        body = request.get_json(silent=True) or {}
        if body.get("balance") is None:
            return jsonify({"ok": False, "error": "balance is required"}), 400
        payload = set_strategy_budget(
            strategy=str(body.get("strategy") or "arbitrage"),
            balance=float(body["balance"]),
            data_dir=Path(body["data_dir"]) if body.get("data_dir") else None,
            mode=request.args.get("mode") or body.get("mode"),
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "arbot"})


def run_dashboard(host: str = "127.0.0.1", port: int | None = None, debug: bool = False) -> None:
    load_dotenv_file(ROOT / ".env")
    app.run(host=host, port=port or PORT, debug=debug, threaded=True)
