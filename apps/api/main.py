from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import health, signals, alerts

app = FastAPI(
    title="Sentinel Grid API",
    description="Tri-signal early warning — clinical + environmental + occupational",
    version="0.1.0",
)

# CORS — allow Vercel preview + prod
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://sentinel-grid.vercel.app",
        "https://*.vercel.app",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["alerts"])

@app.get("/")
def root():
    return {"status": "ok", "service": "sentinel-grid-api"}
