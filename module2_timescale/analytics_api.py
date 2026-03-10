from fastapi import FastAPI

app = FastAPI(title="HQT Timescale Analytics API")

@app.get("/health")
@app.get("/analytics/health")
async def health():
    return {"status": "ok", "module": "timescale_analytics"}
