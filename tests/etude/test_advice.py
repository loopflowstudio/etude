"""Belief-to-strategy comparison adapter and endpoint tests."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from etude.advice import (
    AdviceRequest,
    AdviceRequestIdentity,
    advice_meta,
    compute_deltas,
    load_advice_fixture,
    policy_mass_is_non_uniform,
    request_advice,
)
from etude.server import app
from etude.study_protocol import StudyArtifact

PROTOCOL_DIR = Path(__file__).parents[2] / "protocol"
FIXTURE = json.loads(
    (PROTOCOL_DIR / "fixtures" / "advice-curated-decision.json").read_text(
        encoding="utf-8"
    )
)
RUST_SCHEMA = json.loads(
    (PROTOCOL_DIR / "study-v1.schema.json").read_text(encoding="utf-8")
)
RUST_VALIDATOR = Draft202012Validator(RUST_SCHEMA)

SCENARIO_A = "advice-scenario-a"
SCENARIO_B = "advice-scenario-b"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    load_advice_fixture.cache_clear()
    yield
    load_advice_fixture.cache_clear()


def _expected_identity() -> AdviceRequestIdentity:
    meta = advice_meta()
    return meta.identity


def test_fixture_validates_as_study_artifact_through_model_and_schema() -> None:
    artifact = StudyArtifact.model_validate(FIXTURE["artifact"])
    errors = list(RUST_VALIDATOR.iter_errors(FIXTURE["artifact"]))
    assert not errors
    assert artifact.identity.model.id == "flat-mc-search-v1"
    assert artifact.identity.analysis_budget.id == "1w-8r-16s"


def test_two_scenarios_share_one_decision_with_distinct_non_uniform_evidence() -> None:
    artifact = load_advice_fixture()
    landmarks = artifact.artifact.landmarks
    assert len(landmarks) == 2
    assert len({lm.decision_id for lm in landmarks}) == 1
    assert {lm.id for lm in landmarks} == {SCENARIO_A, SCENARIO_B}
    for landmark in landmarks:
        assert policy_mass_is_non_uniform(landmark)
    mass_a = [row.probability for row in landmarks[0].evidence.policy_mass]
    mass_b = [row.probability for row in landmarks[1].evidence.policy_mass]
    assert mass_a != mass_b


def test_scenarios_cross_reference_landmarks() -> None:
    artifact = load_advice_fixture()
    landmark_ids = {lm.id for lm in artifact.artifact.landmarks}
    scenario_ids = {scenario.landmark_id for scenario in artifact.scenarios}
    assert scenario_ids == landmark_ids
    for scenario in artifact.scenarios:
        assert scenario.label
        assert scenario.inferred_range
        assert scenario.belief_kind
        assert scenario.seed_family


def test_advice_meta_returns_pinned_address_scenarios_and_identity() -> None:
    meta = advice_meta()
    artifact = load_advice_fixture()
    assert meta.address == artifact.artifact.landmarks[0].decision_id
    assert len(meta.scenarios) == 2
    assert meta.identity.source_replay_id == artifact.artifact.identity.source_replay_id
    assert meta.identity.match_id == artifact.artifact.identity.match_id
    assert meta.identity.advisor_id == "flat-mc-search-v1"
    assert meta.identity.compute_id == "1w-8r-16s"


def test_request_advice_returns_real_evidence_and_deltas_for_each_scenario() -> None:
    identity = _expected_identity()
    meta = advice_meta()
    for scenario_id in (SCENARIO_A, SCENARIO_B):
        response = request_advice(meta.address, scenario_id, identity)
        assert response.status == "ok"
        assert response.reason is None
        assert response.address == meta.address
        assert response.frame is not None
        assert response.offers
        assert response.scenario is not None
        assert response.scenario.landmark_id == scenario_id
        assert response.evidence is not None
        assert response.deltas is not None
        # The shared action vocabulary is the frame's legacy offers.
        assert {offer.id for offer in response.offers} == {
            alt.command.offer_id
            for alt in next(
                lm
                for lm in load_advice_fixture().artifact.landmarks
                if lm.id == scenario_id
            ).alternatives
        }


def test_deltas_are_signed_and_non_zero_for_at_least_one_action() -> None:
    artifact = load_advice_fixture()
    landmark_a = next(lm for lm in artifact.artifact.landmarks if lm.id == SCENARIO_A)
    landmark_b = next(lm for lm in artifact.artifact.landmarks if lm.id == SCENARIO_B)
    deltas = compute_deltas(landmark_a.evidence, landmark_b.evidence)
    alternative_ids = {alt.id for alt in landmark_a.alternatives}
    assert set(deltas) == alternative_ids
    for alternative, delta in deltas.items():
        assert set(delta) == {"policy_mass", "search_value", "uncertainty"}
    # The two scenarios disagree on policy mass for at least one action.
    assert any(abs(d["policy_mass"]) > 1e-9 for d in deltas.values())
    # Deltas are antisymmetric: delta(A - B) == -(delta(B - A)).
    reverse = compute_deltas(landmark_b.evidence, landmark_a.evidence)
    for alternative in deltas:
        for metric in ("policy_mass", "search_value", "uncertainty"):
            assert deltas[alternative][metric] == pytest.approx(
                -reverse[alternative][metric]
            )


def test_request_advice_fails_closed_on_identity_mismatch() -> None:
    meta = advice_meta()
    wrong = AdviceRequestIdentity(
        source_replay_id=meta.identity.source_replay_id,
        match_id=meta.identity.match_id,
        advisor_id="not-the-advisor",
        compute_id=meta.identity.compute_id,
    )
    response = request_advice(meta.address, SCENARIO_A, wrong)
    assert response.status == "unavailable"
    assert response.reason == "identity_mismatch"
    assert response.evidence is None
    assert response.deltas is None
    assert response.frame is None


def test_request_advice_fails_closed_on_unknown_scenario() -> None:
    identity = _expected_identity()
    meta = advice_meta()
    response = request_advice(meta.address, "advice-scenario-z", identity)
    assert response.status == "unavailable"
    assert response.reason == "scenario_not_found"
    assert response.evidence is None


def test_request_advice_fails_closed_on_wrong_address() -> None:
    identity = _expected_identity()
    response = request_advice("erd1.wrong", SCENARIO_A, identity)
    assert response.status == "unavailable"
    assert response.reason == "decision_not_found"
    assert response.evidence is None


def test_opponent_hand_identities_absent_from_every_rendered_frame() -> None:
    artifact = load_advice_fixture()
    for landmark in artifact.artifact.landmarks:
        assert not landmark.frame.projection.opponent.hand


def test_get_advice_meta_endpoint_returns_pinned_decision() -> None:
    with TestClient(app) as client:
        response = client.get("/api/advice")
    assert response.status_code == 200
    body = response.json()
    assert body["address"].startswith("erd1.")
    assert len(body["scenarios"]) == 2
    assert body["identity"]["advisor_id"] == "flat-mc-search-v1"


def test_post_advice_endpoint_returns_evidence_and_fails_closed() -> None:
    with TestClient(app) as client:
        meta = client.get("/api/advice").json()
        ok = client.post(
            "/api/advice",
            json={
                "address": meta["address"],
                "scenario_id": SCENARIO_A,
                "identity": meta["identity"],
            },
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["status"] == "ok"
        assert body["evidence"] is not None
        assert body["deltas"] is not None

        wrong_identity = dict(meta["identity"])
        wrong_identity["advisor_id"] = "wrong"
        closed = client.post(
            "/api/advice",
            json={
                "address": meta["address"],
                "scenario_id": SCENARIO_A,
                "identity": wrong_identity,
            },
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "unavailable"
        assert closed.json()["reason"] == "identity_mismatch"
        assert closed.json()["evidence"] is None
