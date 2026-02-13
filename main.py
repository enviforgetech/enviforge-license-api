from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
import os
import secrets

app = FastAPI(title="Enviforge License API")

# =========================
# Storage simples (JSON local no Render)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
TRIALS_PATH = os.path.join(DATA_DIR, "trials.json")

def _ensure_storage() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TRIALS_PATH):
        with open(TRIALS_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def _load_trials() -> dict:
    _ensure_storage()
    with open(TRIALS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_trials(data: dict) -> None:
    _ensure_storage()
    with open(TRIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso_dt(dt_iso: str) -> datetime:
    dt = datetime.fromisoformat(dt_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _is_expired(expires_at_iso: str) -> bool:
    try:
        exp = _parse_iso_dt(expires_at_iso)
        return _utcnow() > exp
    except Exception:
        # Se corromper, trate como expirado (mais seguro)
        return True

# =========================
# Admin (RESET TRIAL) - protegido por token
# =========================
ADMIN_TOKEN = os.getenv("ENVIFORGE_ADMIN_TOKEN", "").strip()
ADMIN_LOG_PATH = os.path.join(DATA_DIR, "admin_resets.log")

def _log_admin(action: str, machine_id: str, detail: dict | None = None) -> None:
    """Log simples em arquivo (auditoria)."""
    _ensure_storage()
    payload = {
        "ts": _utcnow().isoformat(),
        "action": action,
        "machine_id": machine_id,
        "detail": detail or {},
    }
    try:
        with open(ADMIN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

class AdminResetRequest(BaseModel):
    machine_id: str
    product: str = "vmpt"
    reason: str | None = None

# =========================
# Modelos
# =========================
class TrialRequest(BaseModel):
    machine_id: str
    product: str = "vmpt"

class RecoverRequest(BaseModel):
    machine_id: str
    product: str = "vmpt"

class ValidateRequest(BaseModel):
    machine_id: str
    product: str = "vmpt"
    license: str

class ActivateRequest(BaseModel):
    machine_id: str

# =========================
# Helpers de licença
# =========================
def _make_license(machine_id: str, product: str, days: int = 30) -> str:
    """
    Licença simples (MVP):
    ENVIFORGE|<product>|<machine_id>|<exp_iso>|<token>
    """
    exp = (_utcnow() + timedelta(days=days)).isoformat()
    token = secrets.token_urlsafe(24)
    return f"ENVIFORGE|{product}|{machine_id}|{exp}|{token}"

def _parse_license(license_text: str) -> dict:
    parts = license_text.strip().split("|")
    if len(parts) != 5 or parts[0] != "ENVIFORGE":
        raise ValueError("invalid format")

    product = parts[1]
    machine_id = parts[2]
    exp_iso = parts[3]
    token = parts[4]

    exp = datetime.fromisoformat(exp_iso)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    return {"product": product, "machine_id": machine_id, "exp": exp, "token": token}

# =========================
# Health
# =========================
@app.get("/")
def root():
    return {"ok": True}

# =========================
# Trial 30 dias (IDEMPOTENTE quando ainda válido)
# =========================
@app.post("/trial")
def trial(req: TrialRequest):
    """
    Gera licença de teste grátis 30 dias.

    Regras:
    - 1 trial por machine_id.
    - Se já existe e AINDA está válido: retorna novamente a MESMA licença (idempotente).
    - Se já existe e já expirou: retorna conflito (não reinicia 30 dias).
    """
    trials = _load_trials()

    if req.machine_id in trials:
        existing = trials[req.machine_id]

        # Se o product não bater, devolve erro (segurança básica)
        if existing.get("product") != req.product:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Produto não confere para este machine_id.",
                    "issued_at": existing.get("issued_at"),
                    "expires_at": existing.get("expires_at"),
                },
            )

        expires_at = existing.get("expires_at", "")
        if expires_at and not _is_expired(expires_at):
            # ✅ Idempotente: devolve o mesmo trial ainda válido
            return {
                "license": existing.get("license"),
                "expires_at": expires_at,
                "machine_id": req.machine_id,
                "product": req.product,
                "reissued": True,
            }

        # ❌ Trial já expirou: não reemite
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Teste grátis já expirou nesta máquina.",
                "issued_at": existing.get("issued_at"),
                "expires_at": existing.get("expires_at"),
                "reason": "trial_expired",
            },
        )

    # Não existia -> emite trial novo
    lic = _make_license(machine_id=req.machine_id, product=req.product, days=30)
    parsed = _parse_license(lic)

    trials[req.machine_id] = {
        "product": req.product,
        "license": lic,
        "issued_at": _utcnow().isoformat(),
        "expires_at": parsed["exp"].isoformat(),
    }
    _save_trials(trials)

    return {
        "license": lic,
        "expires_at": parsed["exp"].isoformat(),
        "machine_id": req.machine_id,
        "product": req.product,
        "reissued": False,
    }

# =========================
# Recover License (fonte da verdade)
# =========================
@app.post("/recover_license")
def recover_license(req: RecoverRequest):
    """
    Recupera a licença existente desta máquina (sem resetar contagem).

    Comportamento:
    - Se houver trial válido para machine_id: devolve ok=True + payload de licença.
    - Se trial existe mas expirou: ok=False + reason=trial_expired.
    - Se não existe: ok=False + reason=not_found.
    """
    trials = _load_trials()

    if req.machine_id not in trials:
        return {"ok": False, "reason": "not_found", "message": "Nenhuma licença encontrada para esta máquina."}

    existing = trials[req.machine_id]

    if existing.get("product") != req.product:
        return {
            "ok": False,
            "reason": "product_mismatch",
            "message": "Produto não confere para este machine_id.",
            "issued_at": existing.get("issued_at"),
            "expires_at": existing.get("expires_at"),
        }

    expires_at = existing.get("expires_at", "")
    if not expires_at or _is_expired(expires_at):
        return {
            "ok": False,
            "reason": "trial_expired",
            "message": "Teste grátis já expirou nesta máquina.",
            "issued_at": existing.get("issued_at"),
            "expires_at": existing.get("expires_at"),
        }

    # Trial válido -> retorna licença atual
    return {
        "ok": True,
        "license": {
            "license_key": existing.get("license"),
            "status": "active",
            "plan": "trial",
            "expires_at": existing.get("expires_at"),
            "issued_at": existing.get("issued_at"),
            "validated_at": _utcnow().isoformat(),
        },
        "machine_id": req.machine_id,
        "product": req.product,
    }

# =========================
# Validate
# =========================
@app.post("/validate")
def validate(req: ValidateRequest):
    """
    Valida formato + expiração + se a licença pertence à máquina informada.
    """
    try:
        parsed = _parse_license(req.license)
    except Exception:
        raise HTTPException(status_code=400, detail={"message": "Licença inválida."})

    if parsed["product"] != req.product:
        raise HTTPException(status_code=400, detail={"message": "Produto não confere."})

    if parsed["machine_id"] != req.machine_id:
        raise HTTPException(status_code=403, detail={"message": "Licença não pertence a esta máquina."})

    if _utcnow() > parsed["exp"]:
        raise HTTPException(
            status_code=403,
            detail={"message": "Licença expirada.", "expires_at": parsed["exp"].isoformat()},
        )

    return {"status": "valid", "machine_id": req.machine_id, "expires_at": parsed["exp"].isoformat()}

# =========================
# Admin: resetar trial de um machine_id (uso interno)
# =========================
@app.post("/admin/reset_trial")
def admin_reset_trial(
    req: AdminResetRequest,
    token: str = Query(default=""),
    x_admin_token: str = Header(default="", alias="X-Admin-Token"),
):
    """
    Remove o registro de trial do machine_id.

    Segurança:
    - ENVIFORGE_ADMIN_TOKEN definido no Render (Environment).
    - Token pode vir por:
      (1) query: /admin/reset_trial?token=...
      (2) header: X-Admin-Token: ...
    """
    provided = (token or x_admin_token).strip()

    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail={"message": "Admin token não configurado no servidor."})

    if provided != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail={"message": "Não autorizado."})

    trials = _load_trials()
    existed = req.machine_id in trials

    if existed:
        removed = trials.pop(req.machine_id)
        _save_trials(trials)
        _log_admin(
            action="reset_trial",
            machine_id=req.machine_id,
            detail={"product": req.product, "reason": req.reason, "removed": removed},
        )
        return {"ok": True, "message": "Trial resetado.", "machine_id": req.machine_id}
    else:
        _log_admin(
            action="reset_trial_noop",
            machine_id=req.machine_id,
            detail={"product": req.product, "reason": req.reason},
        )
        return {"ok": True, "message": "Não havia trial para esse machine_id.", "machine_id": req.machine_id}

# =========================
# Activate (mantido simples) - exemplo
# =========================
@app.post("/activate")
def activate(req: ActivateRequest):
    lic = _make_license(machine_id=req.machine_id, product="vmpt", days=365)
    return {"status": "activated", "machine_id": req.machine_id, "license": lic}
