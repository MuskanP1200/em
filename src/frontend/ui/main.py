"""
data.py — Frontend API client

Proxies all API calls to the real backend (no mock data toggle).
"""

from typing import Any, Dict, List, Optional

import httpx

from settings import get_settings

BACKEND_URL: str = get_settings().BACKEND_URL


def filter_incidents(search: str = "", status: str = "all") -> List[Dict[str, Any]]:
    """Call the backend /api/incidents and return the list."""
    params = {"status": status, "search": search}
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(
                f"{BACKEND_URL}/api/incidents",
                params=params,
                headers={"Authorization": "Bearer placeholder-token"},
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Backend returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Could not reach backend at {BACKEND_URL}: {e}") from e


def get_incident_detail(incident_id: str) -> Optional[Dict[str, Any]]:
    """
    Call the backend /api/incidents/{id}.
    Returns None on 404, raises on other errors.
    Images arrive as SAS URLs in photo.url — no extra handling needed.
    """
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(
                f"{BACKEND_URL}/api/incidents/{incident_id}",
                headers={"Authorization": "Bearer placeholder-token"},
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Backend returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Could not reach backend at {BACKEND_URL}: {e}") from e


# Feedback


def send_feedback(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST feedback to the backend. Used by the /api/feedback route."""
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{BACKEND_URL}/api/feedback",
                json=payload,
                headers={"Authorization": "Bearer placeholder-token"},
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Backend returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Could not reach backend at {BACKEND_URL}: {e}") from e
