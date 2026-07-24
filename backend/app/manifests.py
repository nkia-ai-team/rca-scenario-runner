"""Optional import boundary for externally maintained dry-run manifests."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.controller import ControllerSpec, parse_controller
from app.models import ExecutionPlan, ExecutionSpec


@dataclass(frozen=True)
class RuntimeManifestScenario:
    id: str
    short_id: str
    controller: ControllerSpec
    injection: dict[str, Any]
    execution: ExecutionPlan


class ScenarioManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str
    id: str = Field(min_length=1)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    readiness: str
    injection: dict[str, Any]
    execution: dict[str, Any]
    actions: dict[str, dict[str, Any]]
    capture_policy: dict[str, Any]

    @model_validator(mode="after")
    def fail_closed(self) -> "ScenarioManifest":
        controller = self.execution.get("controller")
        if not isinstance(controller, dict) or not isinstance(controller.get("live_enabled"), bool):
            raise ValueError("external manifest controller must explicitly set live_enabled")
        for action in ("plan", "run", "cleanup"):
            plan = self.actions.get(action)
            if not isinstance(plan, dict):
                raise ValueError(f"external manifest is missing actions.{action}")
            if plan.get("mode") != "dry-run" or plan.get("mutation") is not False:
                raise ValueError(f"external manifest actions.{action} must be side-effect-free")
        if self.capture_policy.get("create_golden_anomaly") is not False:
            raise ValueError("external manifest capture must not create golden anomaly data")
        if controller["live_enabled"]:
            if self.readiness != "ready":
                raise ValueError("only ready external manifests may enable live execution")
            runtime = controller.get("runtime")
            binding = controller.get("binding")
            if not isinstance(runtime, dict) or not isinstance(binding, dict):
                raise ValueError("live external manifest requires controller.runtime and binding")
            parse_controller(runtime)
            primary = binding.get("primary_ref")
            companions = binding.get("companion_refs", [])
            if not isinstance(primary, str) or not primary:
                raise ValueError("live controller binding requires primary_ref")
            if not isinstance(companions, list) or not all(
                isinstance(item, str) and item for item in companions
            ):
                raise ValueError("live controller companion_refs must be profile ids")
            if runtime.get("mode") == "evaluation" and not isinstance(
                binding.get("approved_profile_id"), str
            ):
                raise ValueError("evaluation binding requires an approved fixed profile id")
        return self

    def runtime_scenario(self) -> RuntimeManifestScenario | None:
        controller = self.execution["controller"]
        if controller["live_enabled"] is not True:
            return None
        binding = controller["binding"]
        return RuntimeManifestScenario(
            id=self.id,
            short_id=self.id,
            controller=parse_controller(controller["runtime"]),  # type: ignore[arg-type]
            injection={
                "profile_id": binding["primary_ref"],
                "companion_profile_ids": list(binding.get("companion_refs", [])),
                "approved_profile_id": binding.get("approved_profile_id"),
                "catalog_slug": self.slug,
            },
            execution=ExecutionPlan(
                orchestrator=ExecutionSpec(
                    transport="local",
                    location="trusted-profile-control",
                    timeout_sec=86400,
                )
            ),
        )


def _root() -> Path | None:
    value = os.environ.get("SCENARIO_MANIFEST_ROOT")
    return Path(value).resolve() if value else None


def load_manifests(root: Path | None = None) -> dict[str, ScenarioManifest]:
    root = root or _root()
    if root is None or not root.is_dir():
        return {}
    catalog: dict[str, ScenarioManifest] = {}
    for path in sorted([*root.glob("*.yaml"), *root.glob("*.yml"), *root.glob("*.json")]):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest = ScenarioManifest.model_validate(data)
        for key in (manifest.slug, manifest.id, manifest.id.lower()):
            if key in catalog and catalog[key].slug != manifest.slug:
                raise ValueError(f"duplicate external scenario manifest key {key}: {path}")
            catalog[key] = manifest
    return catalog


def get_manifest(identifier: str) -> ScenarioManifest | None:
    return load_manifests().get(identifier)


def normalized_manifest_plan(manifest: ScenarioManifest, *, mode: str) -> dict[str, Any]:
    action = manifest.actions[mode]
    controller = manifest.execution["controller"]
    return {
        "valid": True,
        "side_effects": False,
        "scenario_id": manifest.slug,
        "manifest_id": manifest.id,
        "manifest_schema_version": manifest.schema_version,
        "mode": mode,
        "readiness": manifest.readiness,
        "injection": manifest.injection,
        "preflight_checks": list(manifest.execution.get("preflight_ids", [])),
        "controller": controller,
        "adapter_refs": list(action.get("adapter_refs", [])),
        "cleanup": manifest.actions["cleanup"],
        "capture": {
            **manifest.capture_policy,
            "execution": "planned_only",
            "command": None,
        },
        "warnings": [
            (
                "live execution requires compiled digest, fencing, and trusted profile-control"
                if controller.get("live_enabled") is True
                else "external manifest import is plan-only"
            ),
            "dry-run does not contact SSH, Docker, Kubernetes, or API targets",
        ],
    }
