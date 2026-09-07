# Where the tests actually run

The full suite does not run on the Windows workstation and is not meant to.
`pytest-homeassistant-custom-component` pulls in Home Assistant itself, which
wants a POSIX environment and a Python newer than the one on the box. Rather
than fight that, the suite runs on the build VM and the workstation runs only
the parts that need no Home Assistant (`tests/no_ha.py` exists for exactly
that split).

This file records the environment so it does not get rebuilt from scratch a
third time. Building it is not hard; working out *what* to build is, and that
is what keeps getting lost.

## The environment

| | |
|---|---|
| host | `claude@10.10.52.40`, key `~/.ssh/fwbuild_ed25519` |
| working copy | `/work/ha-tuxedo` — a **copy of the tree, not a clone**; it has no `.git` |
| venv | `/work/ha-tuxedo/.venv`, 774 MB, 144 packages |
| interpreter | CPython **3.14.7**, from `uv` at `~/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu` |
| distro python | 3.12.3 (Ubuntu 24.04) — **too old**, do not build the venv from it |
| home assistant | 2026.8.3 |
| harness | `pytest-homeassistant-custom-component` 0.13.357, pytest 9.0.3, pytest-asyncio 1.4.0, pytest-cov 7.1.0 |
| linters | ruff 0.15.21, mypy 1.18.2 |

Two of those rows are the whole point:

- **The interpreter is 3.14 and comes from `uv`, not from apt.** Ubuntu 24.04
  ships 3.12. `pyproject.toml` already pins `python_version = "3.14"` for mypy
  with the reason — Home Assistant 2026.x is written for 3.14 and mypy stops
  inside HA's own source on anything lower. The same applies to running it.
- **`/work/ha-tuxedo` is a copy, so `git` commands there fail.** Edit on the
  workstation and push the changed files over; do not commit from the VM and do
  not expect `git status` to mean anything.

## Running it

```bash
ssh -i ~/.ssh/fwbuild_ed25519 claude@10.10.52.40
cd /work/ha-tuxedo
.venv/bin/python -m pytest -q --cov=custom_components.tuxedo_touch --cov-report=term
```

Last run, 2026-09-06: **280 passed, 99% coverage**, python 3.14.7.

Lint and types, the same way:

```bash
.venv/bin/ruff check .
.venv/bin/mypy custom_components/tuxedo_touch
```

`mypy` is only meaningful with Home Assistant installed, which is true here and
false on the workstation. A local run without it reports `subclassing-Any` and
complains about the `domain=` keyword on `ConfigFlow`; those are artifacts of
the missing package, not defects.

## Rebuilding it, if it is ever lost

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # if uv is not present
uv python install 3.14
cd /work/ha-tuxedo
uv venv --python 3.14 .venv
.venv/bin/pip install homeassistant pytest-homeassistant-custom-component \
                      pytest-cov ruff mypy
```

Pin nothing here on purpose: the harness version has to track whatever Home
Assistant release is current, and a stale pin is how this environment stops
matching CI. If a run disagrees with GitHub Actions, compare versions first.

## What runs on the workstation

Only `tests/no_ha.py` and the tests built on it — `tests/test_push_source.py`
is one. They import the integration's modules directly, with no Home Assistant
and no event loop, so they run anywhere python does. Everything under
`tests/ha/` needs the VM.
