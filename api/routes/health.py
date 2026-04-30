"""Health check routes."""

from fastapi import APIRouter


router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """Return a minimal process health response."""
    return {"status": "ok"}

