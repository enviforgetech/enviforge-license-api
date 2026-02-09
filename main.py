from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
import os
import secrets

app = FastAPI(title="Enviforge License API")

# =========================
# Helpers (armazenamento simples)
# =========================
DATA_DIR = "data"
TRIALS_PATH = os.path.join(DATA_DIR, "trials.json")

def _ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TRIALS_PATH):
        with open(TRIALS_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)

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
        raise ValueError("Formato de licença inválido.")
    _, product, machine_id, exp_iso, token = parts
    exp = datetime.fromisoformat(exp_iso)
    return {
        "product": product,
        "machine_id": machine_id,
        "exp": exp,
        "token": token,
    }


# =========================
# Schemas
# =========================
class TrialRequest(BaseModel):
    machine_id: str
    product: str = "vmpt"

class ValidateRequest(BaseModel):
    machine_id: str
    license: str
    product: str = "vmpt"

class ActivateRequest(BaseModel):
    machine_id: str
    license_key: str


# =========================
# Endpoints básicos
# =========================
@app.get("/")
def root():
    return {"status": "enviforge api online"}

@app.get("/health")
def health():
    return {"ok": True}


# =========================
# Trial 30 dias (NOVO)
# =========================
@app.post("/trial")
def trial(req: TrialRequest):
    """
    Gera licença de teste grátis 30 dias.
    Regra: 1 trial por machine_id (não reinicia 30 dias na mesma máquina).
    """
    trials = _load_trials()

    if req.machine_id in trials:
        # Já existe trial emitido pra essa máquina
        existing = trials[req.machine_id]
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Teste grátis já utilizado nesta máquina.",
                "issued_at": existing.get("issued_at"),
                "expires_at": existing.get("expires_at"),
            },
        )

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
    }


# =========================
# Validate (MELHORADO)
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
        raise HTTPException(status_code=403, detail={"message": "Licença expirada.", "expires_at": parsed["exp"].isoformat()})

    return {"status": "valid", "machine_id": req.machine_id, "expires_at": parsed["exp"].isoformat()}


# =========================
# Activate (mantido simples)
# =========================
@app.post("/activate")
def activate(req: ActivateRequest):
    """
    Mantive seu endpoint, mas padronizei pra JSON no body.
    (Fica mais fácil pro app.)
    """
    return {
        "status": "activated",
        "machine_id": req.machine_id,
        "license": req.license_key
    }
