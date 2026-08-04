"""Minimal readiness wire for the local Evidence V3 profiles.

Readiness is an operational store check only. The authoritative profile is
the ``X-Customs-Service-Mode`` response header; a functional-UAT manifest
stays in the local artifact boundary and is not a release assertion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class EvidenceRuntimeReadinessV3(BaseModel):
    """Small operational response for the versioned readiness endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]


__all__ = ["EvidenceRuntimeReadinessV3"]
