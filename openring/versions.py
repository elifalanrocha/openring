"""Versions of an assistant, and traffic split between them (OpenRing's own; Vapi's API doesn't publish its shape).

`POST /assistant/{id}/version` saves the assistant as it is now: version 1, 2, 3... A version can be restored over
the assistant, or given a share of its calls: `trafficPlan: {"versions": [{"versionId": ..., "weight": 20}]}` on the
assistant sends that percentage of calls to the saved version, the rest to the assistant as it is. Each call keeps
which one answered (`assistantVersion`: a number, or "current"), chosen once when the call starts, so the versions can
be compared on the same days' callers.
"""

from __future__ import annotations

import random
from typing import Any

OWN = ("id", "createdAt", "updatedAt", "trafficPlan", "importedFrom")  # not part of what a version holds


class VersionError(ValueError):
    """A version or traffic plan that can't be used."""


def snapshot(assistant: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in assistant.items() if k not in OWN}


def of(store: Any, assistant_id: str) -> list[dict[str, Any]]:
    """The assistant's versions, oldest first."""
    found = [v for v in store.list("assistant_versions", limit=10_000) if v.get("assistantId") == assistant_id]
    return sorted(found, key=lambda v: v.get("number") or 0)


def save(store: Any, assistant: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    number = max((v.get("number") or 0 for v in of(store, assistant["id"])), default=0) + 1
    return store.create("assistant_versions", {"assistantId": assistant["id"], "number": number,
                                               "name": (name or "").strip()[:80] or f"Version {number}",
                                               "assistant": snapshot(assistant)})


def check_plan(plan: Any, store: Any, assistant_id: str | None) -> None:
    """Raise VersionError unless `plan` is a trafficPlan this assistant can have."""
    if plan is None:
        return
    shares = plan.get("versions") if isinstance(plan, dict) else None
    if not isinstance(shares, list):
        raise VersionError("trafficPlan.versions is a list of {versionId, weight}")
    total = 0.0
    for share in shares:
        weight = share.get("weight") if isinstance(share, dict) else None
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 100:
            raise VersionError("trafficPlan.versions[].weight is a percentage, 0 to 100")
        version = store.get("assistant_versions", str(share.get("versionId") or ""))
        if version is None or version.get("assistantId") != assistant_id:
            raise VersionError(f"trafficPlan: version {share.get('versionId')} is not one of this assistant's")
        total += weight
    if total > 100:
        raise VersionError("trafficPlan: the weights add up to more than 100")


def pick(assistant: dict[str, Any], store: Any, roll: float | None = None) -> tuple[dict[str, Any], Any]:
    """(who answers, its label) for a new call: a saved version by its share of the traffic, else the assistant as it
    is ("current"). `roll` is a number from 0 to 100 (random when not given)."""
    shares = ((assistant.get("trafficPlan") or {}).get("versions")) or []
    roll = random.uniform(0, 100) if roll is None else roll
    upto = 0.0
    for share in shares:
        upto += float(share.get("weight") or 0)
        if roll < upto:
            version = store.get("assistant_versions", str(share.get("versionId") or ""))
            if version is not None:
                return {**version["assistant"], "id": assistant["id"]}, version["number"]
            break  # deleted since: the assistant as it is answers
    return assistant, "current"


def results(store: Any, assistant_id: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each version that answered calls, with what its calls show: how many, success rate, cost and length."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for call in calls:
        if call.get("assistantId") == assistant_id and call.get("status") == "ended":
            groups.setdefault(call.get("assistantVersion") or "current", []).append(call)
    out = []
    for label, mine in groups.items():
        judged = [c["analysis"]["successEvaluation"] for c in mine
                  if (c.get("analysis") or {}).get("successEvaluation") in ("true", "false")]
        costs = [c["cost"] for c in mine if isinstance(c.get("cost"), (int, float))]
        lengths = [float(c.get("durationSeconds") or 0) for c in mine]
        out.append({"version": label, "calls": len(mine),
                    "successRate": round(judged.count("true") / len(judged), 3) if judged else None,
                    "judgedCalls": len(judged),
                    "averageCost": round(sum(costs) / len(costs), 5) if costs else None,
                    "averageSeconds": round(sum(lengths) / len(lengths), 1) if lengths else None})
    return sorted(out, key=lambda r: (r["version"] == "current", str(r["version"])))
