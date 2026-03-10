from fastapi import FastAPI

app = FastAPI(title="HQT Quantum Engine API")

@app.get("/health")
@app.get("/quantum/health")
async def health():
    return {"status": "ok", "module": "quantum_engine"}
