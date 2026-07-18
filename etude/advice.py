"""Belief-to-strategy comparison adapter for the shared decision surface.

This is the first AI-assisted play slice: a narrow, pure-Python consumer of a
checked, identity-pinned advice fixture. It does NOT invoke search at runtime
-- the fixture is the evidence. It validates the request identity against the
fixture's pinned identity, exposes the two belief scenarios and their real
flat-MC evidence at one ``erd1`` decision, and computes the explicit per-action
deltas between them. Identity mismatch, unknown scenario, or wrong address fail
closed to a typed ``unavailable`` state with no evidence.

The adapter is the explicit seam for GAM-4 (live decision address and
retry/compare contracts) and INT-13 (search evidence): ``request_advice`` and
``AdviceRequestIdentity`` are the contract those waves can plug into later
without duplicating the StudyArtifact decision substrate.
"""

from __future__ import annotations

import functools
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field

from etude.experience_protocol import ExperienceFrame, InteractionOffer, ProtocolModel
from etude.study_protocol import DecisionEvidence, StudyArtifact, StudyLandmark

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "protocol" / "fixtures" / "advice-curated-decision.json"


class AdviceRequestIdentity(ProtocolModel):
    """The advisor + compute identity a request claims to incorporate.

    Mirrors the fixture's pinned ``StudyIdentity``: the source replay and match
    must match, the advisor id must match the pinned ``model.id``, and the
    compute id must match the pinned ``analysis_budget.id``. This is the seam
    INT-13's search evidence and GAM-4's live-address contracts bind to.
    """

    source_replay_id: str
    match_id: str
    advisor_id: str
    compute_id: str


class AdviceScenarioSummary(ProtocolModel):
    """Viewer-safe presentation metadata for one belief scenario."""

    landmark_id: str
    label: str
    description: str
    inferred_range: str
    belief_kind: str


class AdviceScenarioRecord(AdviceScenarioSummary):
    """The fixture's scenario metadata, including the pinned seed family.

    ``seed_family`` is generator provenance, not a request field; the adapter
    cross-references each record to a landmark by ``landmark_id``.
    """

    seed_family: str


class AdviceArtifact(ProtocolModel):
    """Wrapper around a ``StudyArtifact`` plus a prototype presentation layer.

    The ``artifact`` is a standard ``StudyArtifact`` (two landmarks at the same
    ``erd1`` decision, each with real flat-MC evidence) and validates through
    both the Pydantic model and the Rust-owned ``study-v1`` schema. The
    ``scenarios`` array is a prototype presentation layer validated here, not by
    the Rust schema.
    """

    artifact: StudyArtifact
    scenarios: list[AdviceScenarioRecord]


class AdviceResponse(ProtocolModel):
    """One advice response: real evidence on success, typed closed on failure."""

    status: Literal["ok", "unavailable"]
    reason: str | None = None
    address: str | None = None
    frame: ExperienceFrame | None = None
    offers: list[InteractionOffer] = Field(default_factory=list)
    scenario: AdviceScenarioSummary | None = None
    evidence: DecisionEvidence | None = None
    deltas: dict[str, dict[str, float]] | None = None
    identity: AdviceRequestIdentity | None = None


class AdviceMeta(ProtocolModel):
    """The pinned decision's bootstrap: address, scenarios, expected identity."""

    address: str
    scenarios: list[AdviceScenarioSummary]
    identity: AdviceRequestIdentity


class AdviceRequest(ProtocolModel):
    """The POST /api/advice request body: one decision, one scenario, one identity."""

    address: str
    scenario_id: str
    identity: AdviceRequestIdentity


@functools.cache
def load_advice_fixture() -> AdviceArtifact:
    """Load and validate the advice fixture once.

    The ``artifact`` validates as a ``StudyArtifact`` (which enforces
    viewer-safety, bindings, and opponent-hand privacy) and the scenarios
    cross-reference landmarks by id.
    """
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    artifact = AdviceArtifact.model_validate(payload)
    landmark_ids = {landmark.id for landmark in artifact.artifact.landmarks}
    scenario_ids = {scenario.landmark_id for scenario in artifact.scenarios}
    if scenario_ids != landmark_ids:
        raise ValueError("advice scenarios do not cover the artifact landmarks")
    if len({landmark.decision_id for landmark in artifact.artifact.landmarks}) != 1:
        raise ValueError("advice landmarks must share one decision address")
    return artifact


def _expected_identity(artifact: AdviceArtifact) -> AdviceRequestIdentity:
    identity = artifact.artifact.identity
    return AdviceRequestIdentity(
        source_replay_id=identity.source_replay_id,
        match_id=identity.match_id,
        advisor_id=identity.model.id,
        compute_id=identity.analysis_budget.id,
    )


def advice_meta() -> AdviceMeta:
    """Return the pinned decision address, scenario summaries, and expected identity.

    Both the live play page and the replay/Study page bootstrap from this so
    they request advice for the same pinned ``erd1`` decision with the same
    identity, then POST ``/api/advice`` per scenario.
    """
    artifact = load_advice_fixture()
    landmark = artifact.artifact.landmarks[0]
    return AdviceMeta(
        address=landmark.decision_id,
        scenarios=[
            AdviceScenarioSummary(
                landmark_id=scenario.landmark_id,
                label=scenario.label,
                description=scenario.description,
                inferred_range=scenario.inferred_range,
                belief_kind=scenario.belief_kind,
            )
            for scenario in artifact.scenarios
        ],
        identity=_expected_identity(artifact),
    )


def compute_deltas(
    left: DecisionEvidence, right: DecisionEvidence
) -> dict[str, dict[str, float]]:
    """Per-action signed deltas (left - right) for policy, value, and uncertainty.

    The two scenarios share one action vocabulary, so alternatives align by id.
    """
    left_policy = {row.alternative: row.probability for row in left.policy_mass}
    right_policy = {row.alternative: row.probability for row in right.policy_mass}
    left_value = {
        row.alternative: row.expected_match_points for row in left.search_value
    }
    right_value = {
        row.alternative: row.expected_match_points for row in right.search_value
    }
    left_unc = {row.alternative: row.standard_error for row in left.uncertainty}
    right_unc = {row.alternative: row.standard_error for row in right.uncertainty}
    deltas: dict[str, dict[str, float]] = {}
    for alternative in left_policy:
        deltas[alternative] = {
            "policy_mass": left_policy[alternative] - right_policy[alternative],
            "search_value": left_value[alternative] - right_value[alternative],
            "uncertainty": left_unc[alternative] - right_unc[alternative],
        }
    return deltas


def _scenario_summary(
    artifact: AdviceArtifact, landmark_id: str
) -> AdviceScenarioSummary:
    scenario = next(
        record for record in artifact.scenarios if record.landmark_id == landmark_id
    )
    return AdviceScenarioSummary(
        landmark_id=scenario.landmark_id,
        label=scenario.label,
        description=scenario.description,
        inferred_range=scenario.inferred_range,
        belief_kind=scenario.belief_kind,
    )


def _unavailable(reason: str, identity: AdviceRequestIdentity) -> AdviceResponse:
    return AdviceResponse(status="unavailable", reason=reason, identity=identity)


def request_advice(
    address: str,
    scenario_id: str,
    identity: AdviceRequestIdentity,
) -> AdviceResponse:
    """Return advice for one scenario at one decision, or fail closed.

    Fail-closed cases (no evidence is ever returned on failure):
    - ``identity_mismatch``: the request identity does not match the fixture's
      pinned identity (source replay, match, advisor, or compute).
    - ``scenario_not_found``: the scenario id is unknown.
    - ``decision_not_found``: the address does not match the fixture's decision.
    """
    artifact = load_advice_fixture()
    expected = _expected_identity(artifact)
    if identity != expected:
        return _unavailable("identity_mismatch", identity)

    landmark = next(
        (lm for lm in artifact.artifact.landmarks if lm.id == scenario_id), None
    )
    if landmark is None:
        return _unavailable("scenario_not_found", identity)
    if landmark.decision_id != address:
        return _unavailable("decision_not_found", identity)

    other = next(lm for lm in artifact.artifact.landmarks if lm.id != scenario_id)
    deltas = compute_deltas(landmark.evidence, other.evidence)
    return AdviceResponse(
        status="ok",
        address=landmark.decision_id,
        frame=landmark.frame,
        offers=list(landmark.frame.offers),
        scenario=_scenario_summary(artifact, scenario_id),
        evidence=landmark.evidence,
        deltas=deltas,
        identity=identity,
    )


def landmark_alternative_ids(landmark: StudyLandmark) -> list[str]:
    """The shared action vocabulary for one landmark, in alternative-id order."""
    return [alternative.id for alternative in landmark.alternatives]


def policy_mass_is_non_uniform(landmark: StudyLandmark) -> bool:
    """True if the landmark's policy mass is not uniform across alternatives."""
    probabilities = [row.probability for row in landmark.evidence.policy_mass]
    if not probabilities:
        return False
    first = probabilities[0]
    return any(not math.isclose(p, first, abs_tol=1e-9) for p in probabilities[1:])
