import os
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from routers import instances, templates

API_KEY = os.getenv("PORTAL_API_KEY", "changeme")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

app = FastAPI(
    title="Aeisoftware SaaS Portal",
    description="K3s Odoo instance provisioning API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

async def verify_api_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key

app.include_router(
    instances.router,
    prefix="/api/instances",
    tags=["Instances"],
    dependencies=[Security(verify_api_key)],
)
app.include_router(
    templates.router,
    prefix="/api/templates",
    tags=["Templates"],
    dependencies=[Security(verify_api_key)],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse("static/index.html")

@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
