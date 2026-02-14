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

def _parse_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

# =========================
# Owner IDs (via env var)
# =========================
OWNER_MIDS_RAW = os.getenv("ENVIFORGE_OWNER_MIDS", "").strip()

def _owner_set() -> set[str]:
    if not OWNER_MIDS_RAW:
        return set()
    items = [x.strip() for x in OWNER_MIDS_RAW.split(",")]
    return {x for x in items if x}

def _is_owner(machine_id: str) -> bool:
    return machine_id.strip() in _owner_set()

OWNER_DAYS = 365 * 20  # 20 anos (aprox) = 7300 dias

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

def _record_and_return(trials: dict, machine_id: str, product: str, lic: str, plan: str, license_type: str):
    parsed = _parse_license(lic)
    trials[machine_id] = {
        "product": product,
        "license": lic,
        "issued_at": trials.get(machine_id, {}).get("issued_at") or _utcnow().isoformat(),
        "expires_at": parsed["exp"].isoformat(),
        "plan": plan,
        "license_type": license_type,
    }
    _save_trials(trials)

    return {
        "license": lic,
        "expires_at": parsed["exp"].isoformat(),
        "machine_id": machine_id,
        "product": product,
        "plan": plan,
        "license_type": license_type,
    }

# =========================
# Health
# =========================
@app.get("/")
def root():
    return {"ok": True}

# =========================
# Trial 30 dias (idempotente) + Owner 20 anos
# =========================
@app.post("/trial")
def trial(req: TrialRequest):
    """
    Trial 30 dias:
    - Se já existe trial e ainda NÃO expirou: retorna o mesmo (idempotente).
    - Se já existe mas expirou: retorna 409 trial_used.
    Owner (ENVIFORGE_OWNER_MIDS):
    - Retorna licença de 20 anos (não consome trial).
    """
    trials = _load_trials()

    # Owner sempre ganha 20 anos
    if _is_owner(req.machine_id):
        existing = trials.get(req.machine_id) or {}
        lic = existing.get("license")
        exp_iso = existing.get("expires_at")
        exp_dt = _parse_dt(exp_iso)

        # Se já tem licença owner válida, reaproveita. Senão, emite de novo.
        if lic and exp_dt and _utcnow() <= exp_dt:
            return {
                "license": lic,
                "expires_at": exp_dt.isoformat(),
                "machine_id": req.machine_id,
                "product": req.product,
                "plan": existing.get("plan") or "owner",
                "license_type": existing.get("license_type") or "owner",
            }

        lic = _make_license(machine_id=req.machine_id, product=req.product, days=OWNER_DAYS)
        return _record_and_return(trials, req.machine_id, req.product, lic, plan="owner", license_type="owner")

    # Trial normal
    if req.machine_id in trials:
        existing = trials[req.machine_id]
        exp_dt = _parse_dt(existing.get("expires_at"))

        # se por algum motivo não tem exp válida, trata como usado
        if not exp_dt:
            raise HTTPException(
                status_code=409,
                detail={"message": "Teste grátis já utilizado nesta máquina.", "expires_at": existing.get("expires_at")},
            )

        # idempotente se ainda válido
        if _utcnow() <= exp_dt:
            return {
                "license": existing.get("license"),
                "expires_at": exp_dt.isoformat(),
                "machine_id": req.machine_id,
                "product": existing.get("product") or req.product,
                "plan": existing.get("plan") or "trial",
                "license_type": existing.get("license_type") or "trial",
            }

        # expirou -> trial usado
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Teste grátis já utilizado nesta máquina.",
                "issued_at": existing.get("issued_at"),
                "expires_at": existing.get("expires_at"),
            },
        )

    lic = _make_license(machine_id=req.machine_id, product=req.product, days=30)
    return _record_and_return(trials, req.machine_id, req.product, lic, plan="trial", license_type="trial")

# =========================
# Recover (recuperar licença desta máquina)
# =========================
@app.post("/recover_license")
def recover_license(req: RecoverRequest):
    """
    Recupera a licença já emitida para este machine_id, se ainda estiver válida.
    - Owner: garante 20 anos (idempotente).
    - Trial: se ainda válido, devolve; se expirado, informa.
    """
    trials = _load_trials()

    # Owner: usa /trial (mesma lógica) de forma segura
    if _is_owner(req.machine_id):
        # reaproveita lógica do /trial
        return trial(TrialRequest(machine_id=req.machine_id, product=req.product))

    existing = trials.get(req.machine_id)
    if not existing:
        raise HTTPException(status_code=404, detail={"message": "Nenhuma licença encontrada para esta máquina."})

    exp_dt = _parse_dt(existing.get("expires_at"))
    if not exp_dt:
        raise HTTPException(status_code=403, detail={"message": "Registro de licença inválido no servidor."})

    if _utcnow() > exp_dt:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Licença expirada.",
                "expires_at": existing.get("expires_at"),
                "plan": existing.get("plan") or "trial",
                "license_type": existing.get("license_type") or "trial",
            },
        )

    return {
        "license": existing.get("license"),
        "expires_at": exp_dt.isoformat(),
        "machine_id": req.machine_id,
        "product": existing.get("product") or req.product,
        "plan": existing.get("plan") or "trial",
        "license_type": existing.get("license_type") or "trial",
    }

# =========================
# Validate
# =========================
@app.post("/validate")
def validate(req: ValidateRequest):
    """
    Valida formato + expiração + se a licença pertence à máquina informada.
    Retorna também 'plan' e 'license_type' quando possível (servidor sabe quando a licença bate com o registro).
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

    # tenta inferir plan/license_type pelo registro (se existir e bater exatamente)
    plan = None
    license_type = None
    try:
        trials = _load_trials()
        rec = trials.get(req.machine_id) or {}
        if rec.get("license") == req.license:
            plan = rec.get("plan")
            license_type = rec.get("license_type")
    except Exception:
        pass

    # fallback: se é owner por env var, marca como owner
    if not plan and _is_owner(req.machine_id):
        plan = "owner"
        license_type = "owner"

    return {
        "status": "valid",
        "machine_id": req.machine_id,
        "expires_at": parsed["exp"].isoformat(),
        "plan": plan,
        "license_type": license_type,
    }

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
