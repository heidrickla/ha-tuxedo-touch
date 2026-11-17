#!/bin/bash
# One gate of .github/workflows/tests.yml, run locally in a venv that holds the
# test requirements: tests | ruff-check | ruff-format | mypy | validate | pure
set -e
cd "$(dirname "$0")/.."
V="${TUX_VENV:-$HOME/venvs/tux}/bin"
case "$1" in
  tests) exec "$V/python" -m pytest -p no:cacheprovider tests -q --cov=custom_components/tuxedo_touch --cov-report=term-missing --cov-fail-under=95 ;;
  ruff-check) exec "$V/ruff" check . ;;
  ruff-format) exec "$V/ruff" format --check . ;;
  mypy) exec "$V/python" -m mypy custom_components/tuxedo_touch ;;
  validate) exec "$V/python" tools/validate_local.py ;;
  pure) exec "$V/python" tests/test_api.py ;;
  *) echo "usage: tools/gate.sh tests|ruff-check|ruff-format|mypy|validate|pure" >&2; exit 2 ;;
esac
