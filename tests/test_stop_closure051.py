"""Pure receipt-error contract checks; no connections, processes or services."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from lab.director.stop_closure import _has_baseline_calibration


def _database_error(sqlstate, message):
    original = RuntimeError("synthetic database error")
    original.sqlstate = sqlstate
    original.diag = SimpleNamespace(message_primary=message)
    return DBAPIError("receipt query", {}, original)


def test_exact_missing_calibration_receipt_is_absence():
    director = MagicMock()
    director.connect.return_value.__enter__.return_value.execute.side_effect = _database_error(
        "P0001", "run has no frozen baseline calibration"
    )
    assert not _has_baseline_calibration(director, uuid4())


@pytest.mark.parametrize(
    "sqlstate,message",
    [
        ("42501", "permission denied for function baseline_calibration_receipt"),
        ("P0001", "calibration receipt requires the Director role"),
        ("08006", "run has no frozen baseline calibration"),
    ],
)
def test_other_receipt_errors_propagate_unchanged(sqlstate, message):
    director = MagicMock()
    error = _database_error(sqlstate, message)
    director.connect.return_value.__enter__.return_value.execute.side_effect = error
    with pytest.raises(DBAPIError) as raised:
        _has_baseline_calibration(director, uuid4())
    assert raised.value is error


def test_present_receipt_has_exact_run_identity():
    director, run_id = MagicMock(), uuid4()
    connection = director.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = {"run_id": str(run_id)}
    assert _has_baseline_calibration(director, run_id)
    statement, parameters = connection.execute.call_args.args
    assert str(statement) == "SELECT lab.baseline_calibration_receipt(:run_id)"
    assert parameters == {"run_id": run_id}
    connection.execute.return_value.scalar_one.return_value = {"run_id": str(uuid4())}
    with pytest.raises(ValueError, match="invalid calibration receipt"):
        _has_baseline_calibration(director, run_id)
