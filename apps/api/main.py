from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import health, signals, alerts, clinical

app = FastAPI(
    title="Sentinel Grid API",
    description="Tri-signal early warning — clinical + environmental + occupational",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://sentinel-grid.*\.vercel\.app|http://localhost:3000",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(clinical.router, prefix="/api/signals/clinical", tags=["clinical"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["alerts"])

@app.get("/")
def root():
    return {"status": "ok", "service": "sentinel-grid-api", "build_marker": "clinical-v1"}
