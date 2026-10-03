"""Stop recovery chooses a durable admission mode without broadening old guards."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import recovery


@pytest.mark.parametrize(
    "status,mode", [("primary_running", None), ("abandoned", "primary_admitted")]
)
def test_attempted_proposal_mode_is_selected_for_live_or_persisted_admission(status, mode):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value = [
        {"status": status, "stop_mode": mode}
    ]
    assert recovery._stopped_proposal_requires_attempted_closure(engine, uuid4(), uuid4())


def test_unattempted_proposal_retains_original_closure_path():
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value = [
        {"status": "proposed", "stop_mode": None}
    ]
    assert not recovery._stopped_proposal_requires_attempted_closure(engine, uuid4(), uuid4())


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"status": "confirmation_running", "stop_mode": None}],
        [{"status": "proposed", "stop_mode": None}] * 2,
    ],
)
def test_unsupported_shape_is_pending_and_never_falls_back_to_zero_admission(rows):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value = rows
    with pytest.raises(recovery.RecoveryPending):
        recovery._stopped_proposal_requires_attempted_closure(engine, uuid4(), uuid4())
