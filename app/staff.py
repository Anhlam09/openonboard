"""SAMPLE staff directory.

Every person here is invented (sample=True). These are names to assign work to, not logins. The reviewer side
is protected by one shared password (REVIEWER_PASSWORD), which can't tell people apart, so these names are
not proof of who acted. The bank replaces this list with individual logins and its real staff directory.
"""

from __future__ import annotations

ROLES = ("banker", "reviewer", "officer")

STAFF: list[dict] = [
    {"id": "staff-maria-lopez", "name": "Maria Lopez", "email": "maria.lopez@example.com", "role": "banker"},
    {"id": "staff-david-chen", "name": "David Chen", "email": "david.chen@example.com", "role": "banker"},
    {"id": "staff-priya-patel", "name": "Priya Patel", "email": "priya.patel@example.com", "role": "reviewer"},
    {"id": "staff-tom-brooks", "name": "Tom Brooks", "email": "tom.brooks@example.com", "role": "reviewer"},
    {"id": "staff-grace-kim", "name": "Grace Kim", "email": "grace.kim@example.com", "role": "officer"},
]


def list_staff() -> list[dict]:
    return [{**s, "sample": True} for s in STAFF]


def get_staff(staff_id: str | None) -> dict | None:
    return next((s for s in list_staff() if s["id"] == staff_id), None)
