"""
Blueprint Flask de solo lectura para consultar la telemetría de Cripto Alertas (`cripto_bot.db`)
mediante autenticación por Bearer Token (`CRIPTO_API_TOKEN`).

Diseñado 100% desacoplado dentro de `cripto/` para facilitar una futura migración
a un servicio/repositorio independiente.

Endpoints:
  - GET /api/cripto/status
  - GET /api/cripto/audit?days=30&format=json|md
  - GET /api/cripto/table/<signals|events|near_misses|ai_reports>?limit=50
"""
import glob
import hmac
import json
import os
import sqlite3
import sys

from flask import Blueprint, Response, jsonify, request

_CRIPTO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CRIPTO_DIR not in sys.path:
    sys.path.insert(0, _CRIPTO_DIR)

from config_cripto import DEFAULT
from cripto_alertas.alerts import load_env
from cripto_alertas.storage import BotStorage

cripto_api_bp = Blueprint("cripto_api", __name__, url_prefix="/api/cripto")

ALLOWED_TABLES = ("signals", "events", "near_misses", "ai_reports")
VALID_ENVS = ("live", "testnet", "paper", "legacy")


def _available_envs() -> list:
    base = DEFAULT.alerts_dir
    found = []
    for name in ("live", "testnet", "paper"):
        if os.path.exists(os.path.join(base, name, "cripto_bot.db")):
            found.append(name)
    if os.path.exists(os.path.join(base, "cripto_bot.db")):
        found.append("legacy")
    return found


def _resolve_env_dir():
    """Resuelve (env_name, alerts_dir) desde ?env=live|testnet|paper|legacy o autodetección."""
    base = DEFAULT.alerts_dir
    req_env = (request.args.get("env") or "").strip().lower()
    if req_env:
        if req_env not in VALID_ENVS:
            return None, None, (jsonify({"error": f"env no válido. Válidos: {list(VALID_ENVS)}"}), 400)
        target_dir = base if req_env == "legacy" else os.path.join(base, req_env)
        return req_env, target_dir, None
    # Autodetección priorizando live -> testnet -> paper -> legacy
    for name in ("live", "testnet", "paper"):
        cand = os.path.join(base, name)
        if os.path.exists(os.path.join(cand, "cripto_bot.db")):
            return name, cand, None
    return "legacy", base, None


def _get_api_token() -> str:
    env = load_env()
    return (os.getenv("CRIPTO_API_TOKEN") or env.get("CRIPTO_API_TOKEN") or "").strip()


def _check_auth():
    expected = _get_api_token()
    if not expected:
        return jsonify({"error": "CRIPTO_API_TOKEN no configurado en el servidor"}), 503
    auth = request.headers.get("Authorization", "")
    provided = ""
    if auth.startswith("Bearer "):
        provided = auth[7:].strip()
    elif request.headers.get("X-API-Key"):
        provided = request.headers.get("X-API-Key", "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _read_open_positions(alerts_dir: str) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(alerts_dir, "state_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for tr in data.get("trades", []):
                if tr.get("status") in ("ABIERTA", "PENDIENTE"):
                    out.append({
                        "signal_id": tr.get("signal_id"),
                        "direction": tr.get("direction"),
                        "status": tr.get("status"),
                        "entry": tr.get("entry"),
                        "sl": tr.get("sl"),
                        "tp": tr.get("tp"),
                        "qty": tr.get("qty"),
                        "fill_price": tr.get("fill_price"),
                        "fill_time": tr.get("fill_time"),
                    })
        except Exception:
            continue
    return out


def _safe_config_summary() -> dict:
    return {
        "universe": list(DEFAULT.universe),
        "min_score": DEFAULT.min_score,
        "wick_reject_min": DEFAULT.wick_reject_min,
        "vol_percentile_min": DEFAULT.vol_percentile_min,
        "trail_start_r": DEFAULT.trail_start_r,
        "trail_distance_r": DEFAULT.trail_distance_r,
        "max_hold_days": DEFAULT.max_hold_days,
        "leverage_max": DEFAULT.leverage_max,
        "max_risk_pct": DEFAULT.max_risk_pct,
        "margin_pct_max": DEFAULT.margin_pct_max,
        "max_portfolio_positions": getattr(DEFAULT, "max_portfolio_positions", 3),
        "capitulation_enabled": getattr(DEFAULT, "capitulation_enabled", True),
    }


def _query_rows(db_path: str, sql: str, params=()) -> list:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@cripto_api_bp.route("/status", methods=["GET"])
def api_status():
    err = _check_auth()
    if err:
        return err
    env_name, alerts_dir, env_err = _resolve_env_dir()
    if env_err:
        return env_err
    st = BotStorage(alerts_dir) if os.path.exists(os.path.join(alerts_dir, "cripto_bot.db")) else None
    counts = {}
    for tbl in ALLOWED_TABLES:
        r = _query_rows(st.db_path, f"SELECT COUNT(*) AS n FROM {tbl}") if st else []
        counts[tbl] = r[0]["n"] if r else 0
    latest_ai = (_query_rows(st.db_path, "SELECT time, kind, model, content FROM ai_reports ORDER BY id DESC LIMIT 1")
                 if st else [])
    return jsonify({
        "ok": True,
        "env": env_name,
        "available_envs": _available_envs(),
        "counts": counts,
        "open_positions": _read_open_positions(alerts_dir),
        "latest_ai_report": latest_ai[0] if latest_ai else None,
        "config": _safe_config_summary(),
    })


@cripto_api_bp.route("/audit", methods=["GET"])
def api_audit():
    err = _check_auth()
    if err:
        return err
    env_name, alerts_dir, env_err = _resolve_env_dir()
    if env_err:
        return env_err
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    fmt = request.args.get("format", "json").lower()
    db_path = os.path.join(alerts_dir, "cripto_bot.db")
    if fmt in ("md", "markdown", "text"):
        if not os.path.exists(db_path):
            return Response(f"# Sin datos todavía para entorno `{env_name}`\n", mimetype="text/markdown; charset=utf-8")
        st = BotStorage(alerts_dir)
        md = st.export_audit_markdown(days=days, cfg=DEFAULT)
        return Response(md, mimetype="text/markdown; charset=utf-8")

    import pandas as pd
    cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).isoformat()
    return jsonify({
        "ok": True,
        "env": env_name,
        "available_envs": _available_envs(),
        "days": days,
        "config": _safe_config_summary(),
        "open_positions": _read_open_positions(alerts_dir),
        "signals": _query_rows(db_path, "SELECT * FROM signals WHERE time >= ? ORDER BY time DESC LIMIT 200", (cutoff,)),
        "events": _query_rows(db_path, "SELECT * FROM events WHERE time >= ? ORDER BY time DESC LIMIT 300", (cutoff,)),
        "near_misses": _query_rows(db_path, "SELECT * FROM near_misses WHERE time >= ? ORDER BY time DESC LIMIT 300", (cutoff,)),
        "ai_reports": _query_rows(db_path, "SELECT * FROM ai_reports WHERE time >= ? ORDER BY time DESC LIMIT 30", (cutoff,)),
    })


@cripto_api_bp.route("/table/<string:name>", methods=["GET"])
def api_table(name: str):
    err = _check_auth()
    if err:
        return err
    env_name, alerts_dir, env_err = _resolve_env_dir()
    if env_err:
        return env_err
    if name not in ALLOWED_TABLES:
        return jsonify({"error": f"Tabla no permitida. Válidas: {list(ALLOWED_TABLES)}"}), 400
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    db_path = os.path.join(alerts_dir, "cripto_bot.db")
    order_col = "time" if name == "signals" else "id"
    rows = _query_rows(db_path, f"SELECT * FROM {name} ORDER BY {order_col} DESC LIMIT ?", (limit,))
    return jsonify({"ok": True, "env": env_name, "table": name, "count": len(rows), "rows": rows})
