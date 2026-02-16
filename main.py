from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
import math

app = FastAPI(title="Enviforge License API")

# =========================
# Storage simples (JSON local no Render)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/tmp")

TRIALS_PATH = os.path.join(DATA_DIR, "trials.json")       # já existe (trial/owner)
LICENSES_PATH = os.path.join(DATA_DIR, "licenses.json")   # NOVO (paid + seats + cooldown)

COOLDOWN_DAYS = 7

def _ensure_storage() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TRIALS_PATH):
        with open(TRIALS_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    if not os.path.exists(LICENSES_PATH):
        with open(LICENSES_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def _load_json(path: str) -> dict:
    _ensure_storage()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: str, data: dict) -> None:
    _ensure_storage()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_trials() -> dict:
    return _load_json(TRIALS_PATH)

def _save_trials(data: dict) -> None:
    _save_json(TRIALS_PATH, data)

def _load_licenses() -> dict:
    return _load_json(LICENSES_PATH)

def _save_licenses(data: dict) -> None:
    _save_json(LICENSES_PATH, data)

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
    email: str
    product: str = "vmpt"
    seats_total: int = 1
    days: int = 365  # default 1 ano (ajuste depois)

class SelfRecoverRequest(BaseModel):
    license: str
    email: str
    new_machine_id: str
    product: str = "vmpt"

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

def _email_norm(s: str) -> str:
    return (s or "").strip().lower()

def _seats_used(rec: dict) -> int:
    return len(rec.get("active_mids") or [])

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
    1) Se a licença existir no LICENSES_PATH (paid): valida por email/seats/active_mids no servidor.
    2) Se não existir: mantém a validação antiga (trial/owner) pelo parse.
    """
    # 1) tenta validar como "paid" pelo registro do servidor
    try:
        licenses_db = _load_licenses()
        paid = licenses_db.get(req.license)
        if paid:
            if paid.get("product") != req.product:
                raise HTTPException(status_code=400, detail={"message": "Produto não confere."})

            exp_dt = _parse_dt(paid.get("expires_at"))
            if not exp_dt:
                raise HTTPException(status_code=403, detail={"message": "Registro de licença inválido no servidor."})

            if _utcnow() > exp_dt:
                raise HTTPException(status_code=403, detail={"message": "Licença expirada.", "expires_at": exp_dt.isoformat()})

            mids = paid.get("active_mids") or []
            if req.machine_id not in mids:
                raise HTTPException(status_code=403, detail={"message": "Esta máquina não está autorizada (seat não vinculado)."})

            return {
                "status": "valid",
                "machine_id": req.machine_id,
                "expires_at": exp_dt.isoformat(),
                "plan": paid.get("plan") or "paid",
                "license_type": paid.get("license_type") or "paid",
            }
    except HTTPException:
        raise
    except Exception:
        # se der qualquer erro lendo paid, cai pro fluxo antigo
        pass

    # 2) fluxo antigo (trial/owner)
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
# Activate (AGORA: gera licença paga + registra no servidor)
# =========================
@app.post("/activate")
def activate(req: ActivateRequest):
    """
    Cria uma licença paga:
    - salva em licenses.json com email, seats_total, active_mids, last_change_at
    - mantém o trial/owner intacto
    """
    if req.seats_total < 1:
        raise HTTPException(status_code=400, detail={"message": "seats_total deve ser >= 1."})
    if req.days < 1:
        raise HTTPException(status_code=400, detail={"message": "days deve ser >= 1."})
    if "@" not in req.email:
        raise HTTPException(status_code=400, detail={"message": "email inválido."})

    lic = _make_license(machine_id=req.machine_id, product=req.product, days=req.days)
    exp = _parse_license(lic)["exp"]

    licenses_db = _load_licenses()
    licenses_db[lic] = {
        "product": req.product,
        "email": _email_norm(req.email),
        "license": lic,
        "issued_at": _utcnow().isoformat(),
        "expires_at": exp.isoformat(),
        "plan": "paid",
        "license_type": "paid",
        "seats_total": int(req.seats_total),
        "active_mids": [req.machine_id],
        "last_change_at": None,
    }
    _save_licenses(licenses_db)

    return {
        "status": "activated",
        "machine_id": req.machine_id,
        "product": req.product,
        "email": _email_norm(req.email),
        "seats_total": int(req.seats_total),
        "expires_at": exp.isoformat(),
        "license": lic,
    }

# =========================
# Self Recover (NOVO) - rebind por email + cooldown 7d
# =========================
@app.post("/self_recover")
def self_recover(req: SelfRecoverRequest):
    """
    Recuperação por e-mail:
    - encontra a licença em licenses.json
    - confere email
    - aplica cooldown de 7 dias
    - emite NOVA licença (com o new_machine_id no texto) mantendo a mesma expiração
    - seta active_mids = [new_machine_id]
    """
    licenses_db = _load_licenses()
    rec = licenses_db.get(req.license)
    if not rec:
        raise HTTPException(status_code=404, detail={"message": "Licença não encontrada no servidor."})

    if rec.get("product") != req.product:
        raise HTTPException(status_code=400, detail={"message": "Produto não confere."})

    if _email_norm(rec.get("email")) != _email_norm(req.email):
        raise HTTPException(status_code=403, detail={"message": "Email não confere com a licença."})

    exp_dt = _parse_dt(rec.get("expires_at"))
    if not exp_dt:
        raise HTTPException(status_code=403, detail={"message": "Registro de licença inválido no servidor."})

    if _utcnow() > exp_dt:
        raise HTTPException(status_code=403, detail={"message": "Licença expirada.", "expires_at": exp_dt.isoformat()})

    last_change = _parse_dt(rec.get("last_change_at"))
    if last_change:
        next_allowed = last_change + timedelta(days=COOLDOWN_DAYS)
        if _utcnow() < next_allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "Troca de máquina em cooldown.",
                    "next_change_allowed_at": next_allowed.isoformat(),
                    "cooldown_days": COOLDOWN_DAYS,
                },
            )

    # mantém mesma expiração: gera licença nova com dias restantes (ceil)
    seconds_left = (exp_dt - _utcnow()).total_seconds()
    days_left = max(1, math.ceil(seconds_left / 86400))

    new_license = _make_license(machine_id=req.new_machine_id, product=req.product, days=days_left)

    # remove a chave antiga e grava a nova (para não ficar duas licenças válidas)
    licenses_db.pop(req.license, None)
    licenses_db[new_license] = {
        **rec,
        "license": new_license,
        "active_mids": [req.new_machine_id],
        "last_change_at": _utcnow().isoformat(),
        "expires_at": exp_dt.isoformat(),  # garante que não "estica" por erro
    }
    _save_licenses(licenses_db)

    return {
        "status": "recovered",
        "message": "Recuperação concluída. Nova licença emitida para a nova máquina.",
        "product": req.product,
        "email": _email_norm(req.email),
        "expires_at": exp_dt.isoformat(),
        "cooldown_days": COOLDOWN_DAYS,
        "license": new_license,
        "machine_id": req.new_machine_id,
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
