#!/bin/sh
set -u

ROOT=/home/cachyos/ai-scientist/data/runtime/parallel-m0/training-doctor
PYTHON=/home/cachyos/ai-scientist/.venv/bin/python
RUFF=/home/cachyos/ai-scientist/.venv/bin/ruff
MYPY=/home/cachyos/ai-scientist/.venv/bin/mypy
PYLINT=/home/cachyos/ai-scientist/.venv/bin/pylint
BANDIT=/home/cachyos/ai-scientist/.venv/bin/bandit

cd "$ROOT" || exit 99
export PYTHONPATH="$ROOT"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

set +e
"$RUFF" check lab/cli.py lab/training tests/test_training_maintenance.py scripts/quality_gate.py
ruff_status=$?
if [ "$ruff_status" -eq 0 ]; then
    "$PYTHON" -m pytest -q tests/test_training_maintenance.py
    pytest_status=$?
else
    pytest_status=125
fi
if [ "$ruff_status" -eq 0 ] && [ "$pytest_status" -eq 0 ]; then
    "$MYPY" --strict lab/training lab/cli.py
    mypy_status=$?
else
    mypy_status=125
fi
if [ "$ruff_status" -eq 0 ] && [ "$pytest_status" -eq 0 ] && [ "$mypy_status" -eq 0 ]; then
    "$PYLINT" lab/training
    pylint_status=$?
else
    pylint_status=125
fi
if [ "$ruff_status" -eq 0 ] && [ "$pytest_status" -eq 0 ] && [ "$mypy_status" -eq 0 ] && [ "$pylint_status" -eq 0 ]; then
    "$BANDIT" -q -r lab/training
    bandit_status=$?
else
    bandit_status=125
fi
printf 'COMMAND_EXIT_CODES ruff=%s pytest=%s mypy=%s pylint=%s bandit=%s\n' \
    "$ruff_status" "$pytest_status" "$mypy_status" "$pylint_status" "$bandit_status"
if [ "$ruff_status" -ne 0 ] || [ "$pytest_status" -ne 0 ] || [ "$mypy_status" -ne 0 ] || [ "$pylint_status" -ne 0 ] || [ "$bandit_status" -ne 0 ]; then
    exit 1
fi
exit 0
