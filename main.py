from fastapi import FastAPI

app = FastAPI(title="Enviforge License API")

@app.get("/")
def root():
    return {"status": "enviforge api online"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/activate")
def activate(machine_id: str, license_key: str):
    return {
        "status": "activated",
        "machine_id": machine_id,
        "license": license_key
    }

@app.post("/validate")
def validate(machine_id: str, license_key: str):
    return {
        "status": "valid",
        "machine_id": machine_id
    }
