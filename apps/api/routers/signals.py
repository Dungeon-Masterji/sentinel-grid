from fastapi import APIRouter
import os, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path="../../.env")
router = APIRouter()


def _parse(d: dict, station_id):
    iaqi = d.get("iaqi", {})
    return {
        "station_id": station_id,
        "station_name": d.get("city", {}).get("name"),
        "aqi": d.get("aqi"),
        "pm25": iaqi.get("pm25", {}).get("v"),
        "pm10": iaqi.get("pm10", {}).get("v"),
        "no2": iaqi.get("no2", {}).get("v"),
        "o3": iaqi.get("o3", {}).get("v"),
        "t_c": iaqi.get("t", {}).get("v"),
        "h_pct": iaqi.get("h", {}).get("v"),
        "time": d.get("time", {}).get("s"),
        "coords": d.get("city", {}).get("geo"),
    }


@router.get("/env/waqi/nearest")
def waqi_nearest(lat: float = 25.25, lon: float = 55.35):
    """Live nearest-station data for a point. Dubai default."""
    token = os.getenv("WAQI_TOKEN")
    r = requests.get(f"https://api.waqi.info/feed/geo:{lat};{lon}/?token={token}").json()
    if r.get("status") != "ok":
        return {"error": "upstream failed", "detail": r}
    return _parse(r["data"], r["data"].get("idx"))


@router.get("/env/waqi/{station_id}")
def waqi_station(station_id: str):
    token = os.getenv("WAQI_TOKEN")
    r = requests.get(f"https://api.waqi.info/feed/@{station_id}/?token={token}").json()
    if r.get("status") != "ok":
        return {"error": "upstream failed", "detail": r}
    return _parse(r["data"], station_id)


@router.get("/env/weather")
def weather(lat: float = 25.25, lon: float = 55.35):
    r = requests.get("https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature",
                "forecast_days": 1}).json()
    h = r["hourly"]
    return {
        "lat": lat, "lon": lon,
        "now": {
            "temp_c": h["temperature_2m"][0],
            "humidity_pct": h["relative_humidity_2m"][0],
            "apparent_temp_c": h["apparent_temperature"][0],
        },
        "timestamps": h["time"][:24],
        "apparent_24h": h["apparent_temperature"][:24],
    }
