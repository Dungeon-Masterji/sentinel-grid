from fastapi import APIRouter
router = APIRouter()

@router.get("/health")
def health():
    return {"status": "healthy", "pillars": ["clinical", "environmental", "occupational"]}
