from fastapi import FastAPI

app = FastAPI(title="HQT Graph API")

@app.get("/health")
@app.get("/graph/health")
async def health():
    return {"status": "ok", "module": "graph_service"}
