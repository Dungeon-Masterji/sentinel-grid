from fastapi import APIRouter
router = APIRouter()

# Placeholder — real detection logic lives in packages/pipelines/fusion/
@router.get("")
def current_alerts():
    return {
        "alerts": [
            {
                "id": "demo-001",
                "severity": "watch",
                "scenario": "heat-stress",
                "location": "Al Quoz Industrial Area",
                "h3": "8b4e63000000000",
                "drivers": {
                    "heat_index": 0.54,
                    "pm25_trend": 0.31,
                    "worker_absence": 0.15,
                },
                "recommended_action": "Extend midday break by 30 min",
                "owner": "Site supervisor",
            }
        ]
    }
