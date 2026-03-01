from fastapi import FastAPI, HTTPException, Header, Query, Request
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta, timezone
import requests
import json
import os
import urllib.request
import urllib.error
import urllib.parse
import secrets

app = FastAPI(title="Enviforge License API")

# =========================
# Email (Resend) - transacional
# =========================
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "Enviforge <no-reply@enviforge.com>").strip()
MAIL_ENABLED = os.getenv("MAIL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

def _mail_should_send() -> bool:
    return bool(MAIL_ENABLED and RESEND_API_KEY and "@" in MAIL_FROM)

def _resend_send_email(*, to_email: str, subject: str, html: str, text: str) -> None:
    """Envia e-mail via Resend. Não levanta exceção para o fluxo principal."""
    if not _mail_should_send():
        return
    payload = {
        "from": MAIL_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url="https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # força leitura para lançar erros HTTP se houver body de erro
            resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<no-body>"
        print(f"[mail] resend HTTPError status={getattr(e, 'code', None)} to={to_email} subject={subject!r} body={body}")
    except urllib.error.URLError as e:
        print(f"[mail] resend URLError to={to_email} subject={subject!r}: {e}")
    except Exception as e:
        # Nunca travar ativação por falha de e-mail
        print(f"[mail] resend failed to={to_email} subject={subject!r}: {e}")

def _license_email_subject(*, product: str, license_type: str) -> str:
    lt = (license_type or "").lower()
    if lt == "trial":
        return f"Sua licença de teste do {product} (30 dias)"
    if lt == "owner":
        return f"Sua licença do {product} está ativa"
    if lt == "paid":
        return f"Sua licença do {product} está ativa"
    if lt == "enterprise":
        return f"Sua licença empresarial do {product} está ativa"
    return f"Sua licença do {product}"

def _license_email_bodies(*, product: str, license_type: str, license_key: str, expires_at_iso: str | None) -> tuple[str, str]:
    """Retorna (html, text). Simples, copy/paste, sem Machine ID."""
    lt = (license_type or "").upper()
    exp_line = expires_at_iso or "-"
    reason = {
        "TRIAL": "Você solicitou o teste do aplicativo.",
        "PAID": "Sua licença foi emitida/ativada.",
        "ENTERPRISE": "Sua licença empresarial foi emitida/ativada.",
        "OWNER": "Sua licença foi emitida/ativada.",
    }.get(lt, "Sua licença foi emitida/ativada.")

    html = f"""<!doctype html>
<html lang='pt-BR'>
<body style='font-family:Arial,Helvetica,sans-serif; background:#f6f7fb; padding:24px;'>
  <div style='max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:24px;border:1px solid #e8e8ee;'>
    <div style='font-size:18px;font-weight:700;margin-bottom:6px;'>Enviforge | {product}</div>
    <div style='color:#555;margin-bottom:18px;'>{reason}</div>
    <div style='background:#f2f3f7;border-radius:10px;padding:14px;margin:16px 0;'>
      <div style='font-size:13px;color:#666;margin-bottom:6px;'>Tipo</div>
      <div style='font-size:15px;font-weight:700;'>{lt}</div>
      <div style='font-size:13px;color:#666;margin:12px 0 6px;'>Validade</div>
      <div style='font-size:14px;'>{exp_line}</div>
    </div>
    <div style='font-size:13px;color:#666;margin:12px 0 6px;'>Sua licença (guarde como backup)</div>
    <pre style='white-space:pre-wrap;word-break:break-word;background:#0b1020;color:#e8eefc;border-radius:10px;padding:14px;font-size:13px;line-height:1.35;'>{license_key}</pre>
    <div style='color:#666;font-size:12px;margin-top:16px;'>Este e-mail é automático. Não responda.</div>
  </div>
</body>
</html>"""

    text = (
        f"Enviforge | {product}\n\n"
        f"{reason}\n\n"
        f"Tipo: {lt}\n"
        f"Validade: {exp_line}\n\n"
        "Sua licença (guarde como backup):\n"
        f"{license_key}\n\n"
        "Este e-mail é automático. Não responda.\n"
    )
    return html, text

def _try_send_license_email(*, to_email: str | None, product: str, license_type: str, license_key: str, expires_at_iso: str | None) -> None:
    if not to_email:
        return
    to_email_norm = _email_norm(to_email)
    if "@" not in to_email_norm:
        return
    subject = _license_email_subject(product=product, license_type=license_type)
    html, txt = _license_email_bodies(product=product, license_type=license_type, license_key=license_key, expires_at_iso=expires_at_iso)
    _resend_send_email(to_email=to_email_norm, subject=subject, html=html, text=txt)

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
# Supabase (REST) - Upsert em public.licenses
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

def _supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

def _supabase_upsert_license(*, machine_id: str, product: str, license_key: str,
                             expires_at: str | None, status: str,
                             email: str | None = None, seats_total: int | None = None) -> None:
    """
    UPSERT em public.licenses usando REST (PostgREST) do Supabase.
    Requer índice/constraint UNIQUE(machine_id, product).
    Best-effort: se falhar, não interrompe o trial.
    """
    if not _supabase_enabled():
        return

    url = f"{SUPABASE_URL}/rest/v1/licenses?on_conflict=machine_id,product"
    payload = {
        "machine_id": machine_id,
        "product": product,
        "license_key": license_key,
        "expires_at": expires_at,   # pode ser None (owner, se quiser)
        "status": status,           # "trial" | "owner" | "active" etc
        "email": email,
        "seats_total": seats_total,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Prefer": "resolution=merge-duplicates",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 201/204 normalmente
            _ = resp.read()
    except Exception as e:
        # Não quebra o trial. Só loga no Render.
        print(f"[WARN] Supabase upsert failed: {e}")

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
    product: str = "psicrocalc"
    reason: str | None = None

# =========================
# Modelos
# =========================
class TrialRequest(BaseModel):
    machine_id: str
    product: str = "psicrocalc"
    email: str | None = None

class RecoverRequest(BaseModel):
    machine_id: str
    product: str = "psicrocalc"

class ValidateRequest(BaseModel):
    machine_id: str
    product: str = "psicrocalc"
    license: str

class ActivateRequest(BaseModel):
    machine_id: str
    email: str
    product: str = "psicrocalc"
    seats_total: int = 1
    days: int = 365  # default 1 ano (ajuste depois)

class SelfRecoverRequest(BaseModel):
    license: str
    email: str
    new_machine_id: str
    product: str = "psicrocalc"

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

def _make_license_with_exp(machine_id: str, product: str, exp: datetime) -> str:
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    token = secrets.token_urlsafe(24)
    return f"ENVIFORGE|{product}|{machine_id}|{exp.isoformat()}|{token}"

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

def _record_and_return(trials: dict, machine_id: str, product: str, lic: str, plan: str, license_type: str, email: str | None = None):
    parsed = _parse_license(lic)
    exp_iso = parsed["exp"].isoformat()
    trials[machine_id] = {
        "product": product,
        "license": lic,
        "issued_at": trials.get(machine_id, {}).get("issued_at") or _utcnow().isoformat(),
        "expires_at": exp_iso,
        "plan": plan,
        "license_type": license_type,
    }
    _save_trials(trials)

    # --- grava também no Supabase (UPSERT) ---
    # status no banco: "trial" ou "owner" (ou "active" no futuro)
    db_status = "owner" if str(license_type).lower() == "owner" else "trial"
    _supabase_upsert_license(
        machine_id=machine_id,
        product=product,
        license_key=lic,
        expires_at=exp_iso,
        status=db_status,
        email=email,
        seats_total=None,
    )

    # --- e-mail transacional (backup) ---
    _try_send_license_email(
        to_email=email,
        product=product,
        license_type=str(license_type).lower(),
        license_key=lic,
        expires_at_iso=exp_iso,
    )

    return {
        "license": lic,
        "expires_at": exp_iso,
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

    resp = _record_and_return(
        trials,
        req.machine_id,
        req.product,
        lic,
        plan="trial",
        license_type="trial",
        email=req.email
    )
    
    # Envio de e-mail (best effort): NÃO pode quebrar a emissão da licença
    try:
        mail_enabled = (os.getenv("MAIL_ENABLED") or "").strip().lower() == "true"
        mail_from = (os.getenv("MAIL_FROM") or "").strip()
        resend_api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    
        if mail_enabled and mail_from and resend_api_key and req.email:
            r = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": mail_from,
                    "to": [req.email],
                    "subject": "Sua licença Trial Enviforge",
                    "text": (
                        "Enviforge — licença Trial\n\n"
                        f"Licença: {resp.get('license')}\n"
                        f"Validade: {resp.get('expires_at')}\n"
                        f"Produto: {resp.get('product')}\n"
                    ),
                },
                timeout=20,
            )
    
            # opcional: guardar retorno pra debug sem atrapalhar o app
            resp["mail"] = {"status": r.status_code}
            try:
                resp["mail"]["body"] = r.json()
            except Exception:
                resp["mail"]["body"] = r.text
    
    except Exception as e:
        resp["mail"] = {"error": str(e)}
    
    return resp
    

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

    # --- e-mail transacional (backup) ---
    _try_send_license_email(
        to_email=req.email,
        product=req.product,
        license_type="paid",
        license_key=lic,
        expires_at_iso=exp.isoformat(),
    )

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
# Pull License (NOVO) - revalidar por email
# =========================
class PullLicenseRequest(BaseModel):
    email: str
    machine_id: str
    product: str = "psicrocalc"

@app.post("/pull_license")
def pull_license(req: PullLicenseRequest):
    """
    Puxa licença válida pelo email.
    - Procura licença paid ativa pelo email + product.
    - Se machine_id já estiver ativo -> retorna.
    - Se houver seat disponível -> adiciona.
    - Se não houver seat -> erro.
    """

    email_norm = _email_norm(req.email)
    licenses_db = _load_licenses()

    # procura licença válida desse email
    for lic_key, rec in licenses_db.items():
        if rec.get("product") != req.product:
            continue
        if _email_norm(rec.get("email")) != email_norm:
            continue

        exp_dt = _parse_dt(rec.get("expires_at"))
        if not exp_dt or _utcnow() > exp_dt:
            continue  # ignorar expiradas

        active_mids = rec.get("active_mids") or []
        seats_total = int(rec.get("seats_total") or 1)

        # se já está ativo nesta máquina
        if req.machine_id in active_mids:
            return {
                "license": lic_key,
                "expires_at": exp_dt.isoformat(),
                "plan": rec.get("plan") or "paid",
                "license_type": rec.get("license_type") or "paid",
                "machine_id": req.machine_id,
            }

        # se ainda há seat disponível
        if len(active_mids) < seats_total:
            active_mids.append(req.machine_id)
            rec["active_mids"] = active_mids
            licenses_db[lic_key] = rec
            _save_licenses(licenses_db)

            return {
                "license": lic_key,
                "expires_at": exp_dt.isoformat(),
                "plan": rec.get("plan") or "paid",
                "license_type": rec.get("license_type") or "paid",
                "machine_id": req.machine_id,
            }

        # não há seat disponível
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Limite de máquinas atingido para esta licença.",
                "seats_total": seats_total,
                "active_mids": active_mids,
            },
        )

    raise HTTPException(
        status_code=404,
        detail={"message": "Nenhuma licença ativa encontrada para este email."},
    )


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
    # mantém mesma expiração: gera licença nova com expiração fixa
    new_license = _make_license_with_exp(
        machine_id=req.new_machine_id,
        product=req.product,
        exp=exp_dt
    )

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

# ========= endpoint admin pra deletar


from pydantic import BaseModel
from fastapi import HTTPException

class AdminDeleteLicenseRequest(BaseModel):
    email: str
    product: str = "psicrocalc"

@app.post("/admin/delete_license")
def admin_delete_license(req: AdminDeleteLicenseRequest):
    email_norm = _email_norm(req.email)
    db = _load_licenses()

    to_delete = []
    for lic_key, rec in db.items():
        if rec.get("product") != req.product:
            continue
        if _email_norm(rec.get("email")) != email_norm:
            continue
        to_delete.append(lic_key)

    if not to_delete:
        raise HTTPException(status_code=404, detail={"message": "Nenhuma licença encontrada para este email/produto."})

    for k in to_delete:
        db.pop(k, None)

    _save_licenses(db)

    return {"deleted": to_delete, "count": len(to_delete)}

#============admin/delete_by_mid

from pydantic import BaseModel
from fastapi import HTTPException

class AdminDeleteByMIDRequest(BaseModel):
    machine_id: str
    product: str = "psicrocalc"

@app.post("/admin/delete_by_mid")
def admin_delete_by_mid(req: AdminDeleteByMIDRequest):
    mid = (req.machine_id or "").strip()
    if not mid:
        raise HTTPException(status_code=422, detail={"message": "machine_id obrigatório"})

    db = _load_licenses()
    to_delete = []

    for lic_key, rec in db.items():
        if rec.get("product") != req.product:
            continue

        # caso 1: a licença guarda machine_id "principal"
        if (rec.get("machine_id") or "").strip() == mid:
            to_delete.append(lic_key)
            continue

        # caso 2: a licença guarda lista de máquinas ativas
        mids = rec.get("active_mids") or []
        if mid in mids:
            to_delete.append(lic_key)

    if not to_delete:
        raise HTTPException(status_code=404, detail={"message": "Nenhuma licença encontrada para este MID/produto."})

    for k in to_delete:
        db.pop(k, None)

    _save_licenses(db)
    return {"deleted": to_delete, "count": len(to_delete), "machine_id": mid}


#=======================admin/delete_by_key

class AdminDeleteByKeyRequest(BaseModel):
    license_key: str

@app.post("/admin/delete_by_key")
def admin_delete_by_key(req: AdminDeleteByKeyRequest):
    key = (req.license_key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail={"message": "license_key obrigatório"})

    db = _load_licenses()
    if key not in db:
        raise HTTPException(status_code=404, detail={"message": "Licença não encontrada."})

    db.pop(key, None)
    _save_licenses(db)
    return {"deleted": key}


# ========================
# Admin: teste de envio de e-mail (Resend)
# ========================
class MailTestIn(BaseModel):
    to_email: EmailStr
    subject: str = "Teste Enviforge"

def _get_admin_token_from_request(request: Request, x_admin_token: str | None) -> str | None:
    # Aceita token via header X-Admin-Token ou query ?token=...
    if x_admin_token:
        return x_admin_token.strip()
    q = request.query_params.get("token")
    return q.strip() if q else None

@app.post("/admin/mail_test")
def admin_mail_test(payload: MailTestIn, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    server_token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not server_token:
        raise HTTPException(status_code=500, detail="Admin token não configurado no servidor.")

    provided = _get_admin_token_from_request(request, x_admin_token)
    if not provided or provided != server_token:
        raise HTTPException(status_code=401, detail="Token admin inválido.")

    mail_enabled = (os.getenv("MAIL_ENABLED") or "").strip().lower() == "true"
    mail_from = (os.getenv("MAIL_FROM") or "").strip()
    resend_api_key = (os.getenv("RESEND_API_KEY") or "").strip()

    if not mail_enabled:
        raise HTTPException(status_code=400, detail="MAIL_ENABLED está desativado.")
    if not mail_from:
        raise HTTPException(status_code=500, detail="MAIL_FROM não configurado.")
    if not resend_api_key:
        raise HTTPException(status_code=500, detail="RESEND_API_KEY não configurada.")

    resend_url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {resend_api_key}",
        "Content-Type": "application/json",
    }

    data = {
        "from": mail_from,                # ex: Enviforge <no-reply@enviforge.com>
        "to": [payload.to_email],
        "subject": payload.subject,
        "text": "Enviforge — teste de envio (mail_test).",
    }

    try:
        r = requests.post(resend_url, headers=headers, json=data, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "Falha ao chamar Resend.", "exception": str(e)})

    # Sempre devolver a verdade nua e crua
    if 200 <= r.status_code < 300:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return {
            "ok": True,
            "mail_enabled": True,
            "mail_from": mail_from,
            "to": payload.to_email,
            "resend_status": r.status_code,
            "resend_body": body,           # aqui deve vir o "id"
        }

    # Erro real do Resend (sem chute)
    try:
        err_body = r.json()
    except Exception:
        err_body = {"raw": r.text}

    raise HTTPException(
        status_code=502,
        detail={
            "ok": False,
            "mail_enabled": True,
            "mail_from": mail_from,
            "to": payload.to_email,
            "resend_status": r.status_code,
            "resend_body": err_body,
        },
    )



#========================
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
