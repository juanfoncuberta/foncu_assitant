"""
Minimal DigitalOcean API client.
Requires DIGITALOCEAN_TOKEN in the environment.
"""

import logging
import os

import httpx

BASE_URL = "https://api.digitalocean.com/v2"
logger = logging.getLogger(__name__)


def _headers() -> dict:
    token = os.environ["DIGITALOCEAN_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def get_balance() -> dict:
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/customers/my/balance", headers=_headers())
        r.raise_for_status()
        return r.json()
