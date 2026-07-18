"""Strict identities and artifact schemas for the manabot skill arena."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArenaKey(StrictModel):
    world: Literal["w2"]
    content_suite: str
    viewer_boundary: str
    arena_version: str
    rating_model_version: str
    rating_prior_sha256: str
    anchor_cohort_sha256: str
    evaluation_compute_envelope_id: str


class SearchSemantics(StrictModel):
    branch_audit: bool = False
    root_prior: str
    leaf_evaluator: str


class PlayerRegistration(StrictModel):
    player_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    display_name: str
    role: Literal["anchor", "incumbent", "challenger"]
    runner_kind: Literal["code", "checkpoint"]
    player_spec: dict[str, Any]
    compute_class_id: str
    information_boundary: str
    world: Literal["w2"]
    observation_abi_sha256: str
    source_sha256: str | None = None
    checkpoint_sha256: str | None = None
    checkpoint_bytes: int | None = None
    parameter_count: int | None = None
    training_seed: int | None = None
    artifact_id: str | None = None
    player_seed_derivation_id: str
    search_call_seed_derivation_id: str | None = None
    search_semantics: SearchSemantics | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "PlayerRegistration":
        if self.runner_kind == "code":
            if not self.source_sha256 or self.checkpoint_sha256 is not None:
                raise ValueError("code players require source_sha256 and no checkpoint")
        else:
            required = (
                self.checkpoint_sha256,
                self.checkpoint_bytes,
                self.parameter_count,
                self.artifact_id,
            )
            if any(value is None for value in required):
                raise ValueError("checkpoint players require complete byte identity")
        if self.player_spec.get("kind") == "determinized_puct":
            required_keys = {
                "sims",
                "worlds",
                "c_puct",
                "max_steps",
                "branch_driver_id",
            }
            if set(self.player_spec) != required_keys | {"kind"}:
                raise ValueError("determinized PUCT spec must be fully explicit")
            if (
                self.search_semantics is None
                or self.search_call_seed_derivation_id is None
            ):
                raise ValueError("determinized PUCT requires search and seed semantics")
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump())


class Schedule(StrictModel):
    deal_seeds: tuple[int, ...]
    competency_seeds: tuple[int, ...]
    bootstrap_seed: int
    bootstrap_replicates: int

    @model_validator(mode="after")
    def validate_schedule(self) -> "Schedule":
        if not self.deal_seeds or len(set(self.deal_seeds)) != len(self.deal_seeds):
            raise ValueError("deal seeds must be nonempty and unique")
        if self.bootstrap_replicates < 1:
            raise ValueError("bootstrap_replicates must be positive")
        return self


class PromotionRule(StrictModel):
    elo_margin: float = 25.0
    elo_lower_bound: float = 0.0
    latency_ratio_max: float = 1.10
    throughput_ratio_min: float = 0.90
    rss_ratio_max: float = 1.10
    competency_margin: float = 0.10
    playout_cap_rate_max: float = 0.001


class ArenaProfile(StrictModel):
    deal_blocks: int
    competency_runs: int
    bootstrap_replicates: int
    disposition: Literal["production", "engineering_smoke_non_promotion"]


class ArenaContract(StrictModel):
    schema_version: Literal[1]
    key: ArenaKey
    anchors: tuple[PlayerRegistration, ...]
    profiles: dict[str, ArenaProfile]
    schedules: dict[str, Schedule]
    promotion: PromotionRule
    source_paths: tuple[str, ...]
    source_sha256: str
    runtime: dict[str, Any]

    @model_validator(mode="after")
    def validate_contract(self) -> "ArenaContract":
        ids = [player.player_id for player in self.anchors]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate anchor player_id")
        if any(player.role != "anchor" for player in self.anchors):
            raise ValueError("contract anchors must have anchor role")
        if canonical_sha256([player.model_dump() for player in self.anchors]) != (
            self.key.anchor_cohort_sha256
        ):
            raise ValueError("anchor cohort digest mismatch")
        for name, profile in self.profiles.items():
            schedule = self.schedules.get(name)
            if schedule is None:
                raise ValueError(f"profile {name} has no schedule")
            if len(schedule.deal_seeds) != profile.deal_blocks:
                raise ValueError(f"profile {name} deal-block mismatch")
            if len(schedule.competency_seeds) != profile.competency_runs:
                raise ValueError(f"profile {name} competency-run mismatch")
            if schedule.bootstrap_replicates != profile.bootstrap_replicates:
                raise ValueError(f"profile {name} bootstrap mismatch")
        return self


class MatchRow(StrictModel):
    arena_key: ArenaKey
    cell_id: str
    deal_block: int
    deal_seed: int
    leg: Literal[0, 1]
    player_a: str
    player_b: str
    player_a_registration_sha256: str
    player_b_registration_sha256: str
    player_a_compute_class: str
    player_b_compute_class: str
    player_a_seed: int
    player_b_seed: int
    player_a_seat: Literal[0, 1]
    winner: int | None
    score_a: float
    decisions: int
    trace_path: str
    trace_sha256: str
    replay_passed: bool
    integrity: dict[str, Any]
