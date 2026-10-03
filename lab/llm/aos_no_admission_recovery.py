"""Closed retained-observation recovery metadata; no runtime authority.

This static validator authenticates nothing and performs no process or database
observation. Trusted future composition must independently authenticate the
current principal, reviewed sources, unchanged scope, canonical committed proof
and physical absence before closing anything. Success here cannot issue another
observation, renew inference, release a GPU, or authorize a database write.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.llm import aos_no_admission_observation as observation
from lab.llm.aos_no_admission_observation import (
    _check,
    _generation,
    _observer,
    _original,
    _proofs,
    _require,
)

# Exact concrete types reject bool-as-int and expectation subclasses.
# pylint: disable=unidiomatic-typecheck

NAME = "aos-scientist-no-admission-observation-recovery.v1"
FEATURE = "no-admission-observation-recovery.v1"
VERSION = 1
PURPOSE = "close_retained_no_admission"
MAX_AGE_US = observation.MAX_AGE_US
SCHEMA_PATH = (
    Path(__file__).parent / "contracts/no_admission_observation_recovery_v1/recovery.schema.json"
)
OBSERVATION_SCHEMA_SHA256 = "92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63"


def schema_document() -> dict[str, Any]:
    """Read a standalone closed schema with unchanged inherited definitions."""
    document = observation.decode(SCHEMA_PATH.read_bytes(), exact=False)
    old = observation.schema_document()
    _require(observation.digest(old) == OBSERVATION_SCHEMA_SHA256, "historical schema changed")
    for name in ("ScientistServerGeneration", "hash", "id", "store", "target", "cleanup_scope"):
        _require(
            document["$defs"][name] == old["$defs"][name], "inherited recovery definition changed"
        )
    return document


def schema_hash() -> str:
    """SHA of the full canonical schema, including recursive definitions."""
    return observation.digest(schema_document())


def source_hash() -> str:
    """Fingerprint of source bytes; not an authorization decision."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class RecoveryExpectations:
    """Independently supplied pins and preimages, never self-authentication.

    Authority SHA covers the entire canonical context; raw SHA covers its exact
    bytes. No self-hash field or signing credential appears in the wire object.
    Copying these expectations from an incoming object provides no evidence.
    """

    schema_sha256: str
    authority_sha256: str
    authority_raw_sha256: str
    observation_raw_sha256: str
    observation: observation.ObservationExpectations
    canonical_schema_sha256: str
    principal: bytes


def _retained(raw: bytes, expected: RecoveryExpectations) -> dict[str, Any]:
    """Validate historical bytes without pretending their old TTL is current."""
    _require(
        type(expected.observation) is observation.ObservationExpectations,
        "independent historical expectations required",
    )
    old_expected = expected.observation
    document = observation.schema_document()
    _require(
        old_expected.schema_sha256 == OBSERVATION_SCHEMA_SHA256 == observation.digest(document),
        "historical observation schema pin mismatch",
    )
    value = observation.decode(raw)
    _require(
        hashlib.sha256(raw).hexdigest() == expected.observation_raw_sha256,
        "retained observation raw pin mismatch",
    )
    _check(value, document, document["$defs"])
    for field in ("original", "observer", "canonical", "physical", "cleanup_scope"):
        preimage = getattr(old_expected, field)
        observation.decode(preimage)
        _require(
            observation.canonical(value[field]) == preimage,
            "independent historical " + field + " preimage mismatch",
        )
    _original(value)
    _observer(value, old_expected.schema_sha256)
    _proofs(value)
    freshness = value["freshness"]
    _require(
        0 < freshness["expires_boottime_us"] - freshness["observed_boottime_us"] <= MAX_AGE_US,
        "historical observation TTL malformed",
    )
    _require(
        value["observation_sha256"]
        == observation.digest(
            {key: item for key, item in value.items() if key != "observation_sha256"}
        ),
        "historical observation digest mismatch",
    )
    return value


def validate_recovery(
    raw: bytes,
    *,
    retained_observation: bytes,
    expected: RecoveryExpectations,
    now_boottime_us: int,
    current_boot_id: str,
) -> dict[str, Any]:
    """Check static consistency only; no callback, live authority or closure.

    The original proof's exact bytes and timestamps are preserved. Its observer
    is historical; the distinct recovery principal needs independent runtime
    authentication outside this module. No historical clock is synthesized.
    """
    _require(type(expected) is RecoveryExpectations, "independent expectations required")
    document = schema_document()
    _require(expected.schema_sha256 == observation.digest(document), "recovery schema pin mismatch")
    value = observation.decode(raw)
    _check(value, document, document["$defs"])
    _require(
        hashlib.sha256(raw).hexdigest() == expected.authority_raw_sha256
        and observation.digest(value) == expected.authority_sha256,
        "recovery authority pin mismatch",
    )
    old = _retained(retained_observation, expected)
    retained = {
        "observation_raw_sha256": expected.observation_raw_sha256,
        "observation_sha256": old["observation_sha256"],
        "observation_schema_sha256": expected.observation.schema_sha256,
        "canonical_store": old["canonical"]["store"],
        "canonical_schema_sha256": expected.canonical_schema_sha256,
        "cleanup_scope_sha256": observation.digest(old["cleanup_scope"]),
        "observer_generation_sha256": old["observer"]["generation_sha256"],
        "producer_source_sha256": old["observer"]["source_sha256"],
        "producer_config_sha256": old["observer"]["config_sha256"],
    }
    _require(value["retained"] == retained, "retained recovery pins differ")
    _require(
        value["target"] == old["target"]
        and value["cleanup_scope"] == old["cleanup_scope"]
        and value["recovery_request_id"] != old["target"]["request_id"],
        "recovery target or unchanged cleanup scope differs",
    )
    principal = value["principal"]
    observation.decode(expected.principal)
    _require(
        observation.canonical(principal) == expected.principal
        and principal["generation_sha256"] == observation.digest(principal["generation"])
        and principal["generation_sha256"]
        not in {
            old["target"]["original_caller_generation_sha256"],
            old["original"]["broker_generation_sha256"],
            old["observer"]["generation_sha256"],
        },
        "independent distinct recovery principal mismatch",
    )
    _generation(principal["generation"])
    # CallerGeneration includes parent fields that ServerGeneration omits.
    # Comparing full-object hashes alone could accept that same live process.
    identity_fields = ("boot_id", "uid", "pid", "start_ticks")
    principal_identity = tuple(principal["generation"][key] for key in identity_fields)
    historical_generations = (
        old["original"]["admission_record"]["admission_binding"]["caller_generation"],
        old["original"]["broker_generation"],
        old["observer"]["generation"],
    )
    _require(
        all(
            principal_identity != tuple(generation[key] for key in identity_fields)
            for generation in historical_generations
        ),
        "recovery principal reuses a historical process identity",
    )
    freshness = value["freshness"]
    _require(
        type(now_boottime_us) is int and 0 <= now_boottime_us <= observation.SAFE_INTEGER,
        "current boottime requires bounded integer microseconds",
    )
    _require(
        type(current_boot_id) is str
        and current_boot_id
        == freshness["boot_id"]
        == principal["generation"]["boot_id"]
        == old["freshness"]["boot_id"]
        == old["observer"]["generation"]["boot_id"]
        == old["original"]["broker_generation"]["boot_id"],
        "retained recovery boot mismatch",
    )
    issued, expires = freshness["issued_boottime_us"], freshness["expires_boottime_us"]
    _require(
        old["freshness"]["observed_boottime_us"] <= issued <= now_boottime_us < expires
        and 0 < expires - issued <= MAX_AGE_US,
        "stale/future/unbounded recovery freshness",
    )
    return value
