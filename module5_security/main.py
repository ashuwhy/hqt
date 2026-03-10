from fastapi import FastAPI

app = FastAPI(title="HQT Security Proxy")

@app.get("/health")
async def health():
    return {"status": "ok", "module": "security_proxy"}

@app.get("/")
async def root():
    return {"message": "HQT Security Proxy is running"}
