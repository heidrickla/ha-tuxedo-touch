"""Local stand-in for the checks CI would run.

hassfest and the HACS action run on GitHub; this approximates the parts of
them that can be checked with no network at all, plus the cross-file
consistency that nothing else checks: entity translation keys against
strings.json, exceptions raised against exceptions declared, setup and poll
exceptions carrying translation keys, the quality scale against the pinned
rule list. Run it before a push so the push is not the first verification.

    python tools/validate_local.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from typing import Any

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DOMAIN = "tuxedo_touch"
COMP = os.path.join(ROOT, "custom_components", DOMAIN)
PLATFORMS = ("alarm_control_panel",)

# In-repo brand images are served by Home Assistant's brands component from
# this release on; below it the integration has no icon at all.
BRANDS_IN_REPO_SINCE = (2026, 3, 0)

# Exceptions raised from setup and polling whose text reaches the integration
# card. Each must carry a translation key rather than an f-string.
SETUP_EXCEPTIONS = {
    "ConfigEntryNotReady",
    "ConfigEntryAuthFailed",
    "ConfigEntryError",
    "UpdateFailed",
}

# hassfest requires these for a custom integration.
REQUIRED_MANIFEST = [
    "domain",
    "name",
    "documentation",
    "codeowners",
    "iot_class",
    "version",
]
VALID_IOT_CLASS = {
    "assumed_state",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
    "calculated",
}

# Pinned from developers.home-assistant.io/docs/core/integration-quality-scale/checklist
# (checked 2026-09-02: 54 rules, none new or deprecated). The list is pinned
# here on purpose: a quality_scale.yaml that is missing a rule reads as
# complete, and checking against the full list turns an omission into a
# failure.
ALL_RULES = {
    # Bronze
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    # Silver
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
    # Gold
    "devices",
    "diagnostics",
    "discovery-update-info",
    "discovery",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
    # Platinum
    "async-dependency",
    "inject-websession",
    "strict-typing",
}

failures: list[str] = []
notes: list[str] = []


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def read_json(*parts: str) -> Any:
    return json.loads(read(*parts))


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def constants(source: str, prefix: str) -> dict[str, str]:
    """Module-level string assignments whose name starts with prefix."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id.startswith(prefix)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target.id] = node.value.value
    return found


def untranslated_raises(source: str) -> list[str]:
    """`raise X(...)` of a setup or poll exception without a translation key."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name not in SETUP_EXCEPTIONS:
            continue
        keywords = {kw.arg for kw in node.exc.keywords}
        if "translation_key" not in keywords:
            bad.append(f"{name} at line {node.lineno}")
    return bad


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def quoted_placeholders(node: Any, path: str = "") -> list[str]:
    """Dotted paths whose text wraps a placeholder in single quotes.

    The frontend reads single quotes as ICU escaping, so a quoted placeholder
    is printed as its own name instead of the value. hassfest rejects it.
    """
    bad: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            bad.extend(quoted_placeholders(value, f"{path}.{key}" if path else key))
    elif isinstance(node, str) and re.search(r"'[^']*\{\w+\}[^']*'", node):
        bad.append(path)
    return bad


def main() -> int:
    manifest = read_json(COMP, "manifest.json")
    const_src = read(COMP, "const.py")
    strings = read_json(COMP, "strings.json")

    # ---------------------------------------------------------- manifest
    for key in REQUIRED_MANIFEST:
        check(key in manifest, f"manifest.json missing required key {key!r}")
    check(
        manifest.get("domain") == DOMAIN,
        f"manifest domain is {manifest.get('domain')!r}",
    )
    check(
        manifest.get("iot_class") in VALID_IOT_CLASS,
        f"manifest iot_class {manifest.get('iot_class')!r} is not a valid value",
    )
    check(
        isinstance(manifest.get("codeowners"), list)
        and all(c.startswith("@") for c in manifest["codeowners"]),
        "manifest codeowners entries must start with @",
    )
    keys = list(manifest)
    check(
        keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:]),
        "manifest keys must be domain, name, then alphabetical (hassfest MANIFEST)",
    )
    if re.search(
        r"//(?:localhost|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)",
        manifest.get("documentation", ""),
    ):
        notes.append("documentation URL points at a LAN host - useless to a user")
    check(
        "quality_scale" not in manifest,
        "quality_scale in manifest.json: the badge is core-only, a custom "
        "integration builds to the rules and does not claim a tier",
    )
    for req in manifest.get("requirements", []):
        check(" " not in req, f"requirement {req!r} contains a space")

    # Every place a version is written must agree with the manifest, or Home
    # Assistant reports one number and HACS another.
    const_version = constants(const_src, "VERSION").get("VERSION")
    if const_version is not None:
        check(
            const_version == manifest.get("version"),
            f"const.VERSION {const_version!r} != manifest version "
            f"{manifest.get('version')!r}",
        )
    pyproject_path = os.path.join(ROOT, "pyproject.toml")
    if os.path.isfile(pyproject_path):
        import tomllib

        project = tomllib.loads(read(pyproject_path)).get("project", {})
        if "version" in project:
            check(
                project["version"] == manifest.get("version"),
                f"pyproject version {project['version']!r} != manifest version "
                f"{manifest.get('version')!r}",
            )
        else:
            notes.append("pyproject.toml carries no version; manifest is the only one")

    # ---------------------------------------------------------- hacs.json
    hacs = read_json(ROOT, "hacs.json")
    check("name" in hacs, "hacs.json must contain name")

    # ---------------------------------------------------------- brand images
    brand = os.path.join(COMP, "brand")
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        check(os.path.isfile(os.path.join(brand, name)), f"missing brand/{name}")
    if os.path.isdir(brand):
        floor = hacs.get("homeassistant", "0")
        check(
            version_tuple(floor) >= BRANDS_IN_REPO_SINCE,
            f"hacs.json homeassistant {floor!r} is below "
            f"{'.'.join(map(str, BRANDS_IN_REPO_SINCE))}, the first release "
            "that serves the in-repo brand images",
        )

    # ---------------------------------------------------------- translations
    en = read_json(COMP, "translations", "en.json")
    check(
        strings == en,
        "strings.json and translations/en.json differ - copy strings.json over",
    )
    quoted = quoted_placeholders(strings)
    check(
        not quoted,
        f"strings.json quotes placeholders at {quoted} - drop the single quotes",
    )

    # ---------------------------------------------------------- actions
    # This integration registers no actions of its own, so there is nothing
    # to describe; a services.yaml or a services block appearing would mean
    # one was added without the rest.
    check(
        not os.path.isfile(os.path.join(COMP, "services.yaml")),
        "services.yaml exists but no action is registered in async_setup",
    )
    check(
        "services" not in strings,
        "strings.json describes services but none are registered",
    )
    check(
        not constants(const_src, "SERVICE_"),
        "const.py names SERVICE_ constants but no action is registered",
    )

    # ---------------------------------------------------------- quality scale
    scale_path = os.path.join(COMP, "quality_scale.yaml")
    check(os.path.isfile(scale_path), "quality_scale.yaml is missing")
    if os.path.isfile(scale_path):
        try:
            import yaml

            declared = yaml.safe_load(read(scale_path)).get("rules", {})
            missing = ALL_RULES - set(declared)
            check(not missing, f"quality_scale.yaml does not mention {sorted(missing)}")
            unknown = set(declared) - ALL_RULES
            check(not unknown, f"quality_scale.yaml invents rules {sorted(unknown)}")
            for rule, value in sorted(declared.items()):
                if isinstance(value, dict):
                    check(
                        value.get("status") in {"done", "todo", "exempt"},
                        f"{rule}: status must be done/todo/exempt",
                    )
                    if value.get("status") != "done":
                        check(
                            bool(str(value.get("comment", "")).strip()),
                            f"{rule}: a non-done status needs a comment saying why",
                        )
                else:
                    check(value == "done", f"{rule}: bare value must be 'done'")
            todo = sorted(
                r
                for r, v in declared.items()
                if isinstance(v, dict) and v.get("status") == "todo"
            )
            if todo:
                notes.append(f"quality scale still todo: {', '.join(todo)}")
        except ImportError:
            notes.append("PyYAML not installed - quality_scale.yaml not parsed")

    # ---------------------------------------------------- entity translations
    # Every translation key an entity uses needs a name, and every name needs
    # an entity using it. Both forms are matched: the class attribute and the
    # EntityDescription keyword. icons.json is optional for this domain (the
    # alarm panel uses the frontend's state icons) but is checked if present.
    icons_path = os.path.join(COMP, "icons.json")
    icons = read_json(icons_path) if os.path.isfile(icons_path) else None
    key_re = re.compile(r'(?:_attr_translation_key\s*=|\btranslation_key=)\s*"([^"]+)"')
    # An exception is raised with translation_domain=DOMAIN right before its
    # key; entity and issue keys never carry translation_domain. Subtracted
    # here so an error raised inside a platform file is not read as one of
    # that platform's entities.
    exc_re = re.compile(r'translation_domain=DOMAIN,\s*translation_key="([^"]+)"')
    for platform in PLATFORMS:
        source = read(COMP, f"{platform}.py")
        used = set(key_re.findall(source)) - set(exc_re.findall(source))
        named = set(strings.get("entity", {}).get(platform, {}))
        check(used == named, f"{platform}: names {sorted(named ^ used)} out of step")
        if icons is not None:
            declared_icons = set(icons.get("entity", {}).get(platform, {}))
            check(
                used == declared_icons,
                f"{platform}: icons {sorted(declared_icons ^ used)} out of step",
            )
    # A name with a placeholder needs the entity to supply it.
    for platform, entries in strings.get("entity", {}).items():
        source = read(COMP, f"{platform}.py")
        for key, spec in entries.items():
            for placeholder in re.findall(r"\{(\w+)\}", spec.get("name", "")):
                check(
                    f'"{placeholder}"' in source,
                    f"{platform}.{key}: name placeholder {{{placeholder}}} is "
                    "never supplied by the entity",
                )

    # ------------------------------------------------- exception translations
    raised: set[str] = set()
    for f in sorted(os.listdir(COMP)):
        if f.endswith(".py"):
            raised |= set(exc_re.findall(read(COMP, f)))
    declared_exc = set(strings.get("exceptions", {}))
    check(
        raised <= declared_exc,
        f"code raises undeclared exception keys {sorted(raised - declared_exc)}",
    )
    check(
        declared_exc <= raised,
        f"strings.json declares unused exceptions {sorted(declared_exc - raised)}",
    )
    # Setup and poll failures are shown on the integration card, so they must
    # be translated too - an f-string there is the shape of the silent miss.
    for f in ("__init__.py", "coordinator.py"):
        if os.path.isfile(os.path.join(COMP, f)):
            for bad in untranslated_raises(read(COMP, f)):
                failures.append(f"{f}: {bad} is raised without a translation_key")

    # ----------------------------------------------------- issue translations
    issue_consts = set(constants(const_src, "ISSUE_").values())
    declared_issues = set(strings.get("issues", {}))
    check(
        issue_consts == declared_issues,
        f"const.py issues {sorted(issue_consts)} != strings.json issues "
        f"{sorted(declared_issues)}",
    )
    # An issue the user can act on needs a fix flow, and a fix flow needs the
    # repairs component loaded: a missing dependency is a runtime failure the
    # moment the user clicks the notification.
    sources = {f: read(COMP, f) for f in sorted(os.listdir(COMP)) if f.endswith(".py")}
    created = {
        name
        for src in sources.values()
        for name in re.findall(
            r"async_create_issue\((?:.|\n)*?translation_key=(\w+)", src
        )
    }
    unknown_issues = created - set(constants(const_src, "ISSUE_"))
    check(
        not unknown_issues,
        f"async_create_issue uses keys {sorted(unknown_issues)} that const.py "
        "does not define as ISSUE_ constants",
    )
    fixable = any("is_fixable=True" in src for src in sources.values())
    repairs_path = os.path.join(COMP, "repairs.py")
    if fixable:
        check(
            os.path.isfile(repairs_path),
            "an issue is created with is_fixable=True but repairs.py is missing",
        )
        check(
            "repairs" in manifest.get("dependencies", []),
            "a fix flow needs 'repairs' in the manifest's dependencies",
        )
    if os.path.isfile(repairs_path):
        check(
            "async_create_fix_flow" in read(repairs_path),
            "repairs.py defines no async_create_fix_flow",
        )
        # Every step and abort a fix flow can reach needs its own text under
        # the issue's fix_flow, or the user gets a raw key.
        repairs_src = read(repairs_path)
        for key in issue_consts:
            flow = strings.get("issues", {}).get(key, {}).get("fix_flow")
            if flow is None:
                continue
            steps = set(flow.get("step", {}))
            aborts = set(flow.get("abort", {}))
            used_steps = set(re.findall(r'step_id="([^"]+)"', repairs_src))
            used_aborts = set(re.findall(r'reason="([^"]+)"', repairs_src))
            check(
                used_steps <= steps,
                f"issues.{key}.fix_flow is missing steps {sorted(used_steps - steps)}",
            )
            check(
                used_aborts <= aborts,
                f"issues.{key}.fix_flow is missing aborts "
                f"{sorted(used_aborts - aborts)}",
            )

    # ------------------------------------------------------ dhcp discovery
    # The manifest's dhcp block and the flow step that serves it are one
    # mechanism: a matcher with no step makes Home Assistant start a flow
    # that lands on the user form, and a step with no matcher never runs.
    flow_src = read(COMP, "config_flow.py")
    dhcp_matchers = manifest.get("dhcp", [])
    check(
        bool(dhcp_matchers) == ("async_step_dhcp" in flow_src),
        "manifest dhcp matchers and config_flow.async_step_dhcp must go together",
    )
    if any(m.get("registered_devices") for m in dhcp_matchers):
        # registered_devices matches against the device registry, so a device
        # without the MAC as a connection is never discovered at all.
        check(
            any("CONNECTION_NETWORK_MAC" in read(COMP, f"{p}.py") for p in PLATFORMS),
            "dhcp registered_devices needs the panel's MAC in device info as a "
            "CONNECTION_NETWORK_MAC connection",
        )
    # Every abort the flow can reach needs its text, or the user reads a key.
    declared_aborts = set(strings.get("config", {}).get("abort", {}))
    flow_aborts = set(re.findall(r'reason="([^"]+)"', flow_src))
    check(
        flow_aborts <= declared_aborts,
        f"config.abort is missing {sorted(flow_aborts - declared_aborts)}",
    )

    # ------------------------------------------------------ strict typing
    # strict-typing is claimed on pyproject.toml saying strict with nothing
    # switched off under it. An override that relaxes a check would leave the
    # config still reading as strict at a glance while the claim went false.
    if os.path.isfile(pyproject_path):
        import tomllib

        mypy_cfg = tomllib.loads(read(pyproject_path)).get("tool", {}).get("mypy", {})
        check(
            mypy_cfg.get("strict") is True,
            "pyproject.toml [tool.mypy] does not set strict = true",
        )
        # Supplying types for a dependency that ships none is not a relaxation
        # of this integration's own checking; switching a check off is.
        for override in mypy_cfg.get("overrides", []):
            relaxations = set(override) - {"module", "ignore_missing_imports"}
            check(
                not relaxations,
                f"the mypy override for {override.get('module')!r} relaxes "
                f"strict with {sorted(relaxations)}",
            )

    # ----------------------------------------------------- the coverage gate
    # test-coverage is claimed on a threshold CI enforces. A workflow that
    # only reports coverage would let the claim rot silently.
    workflow = os.path.join(ROOT, ".github", "workflows", "tests.yml")
    if os.path.isfile(workflow):
        check(
            "--cov-fail-under=95" in read(workflow),
            "the GitHub Tests workflow does not gate coverage at 95%",
        )

    # ------------------------------------------------------------ platforms
    init_src = read(COMP, "__init__.py")
    for platform in PLATFORMS:
        check(
            f"Platform.{platform.upper()}" in init_src,
            f"{platform}.py exists but Platform.{platform.upper()} is not forwarded",
        )
        check(
            "PARALLEL_UPDATES" in read(COMP, f"{platform}.py"),
            f"{platform}.py does not set PARALLEL_UPDATES",
        )
    # A file in the package that Home Assistant will never load is clutter
    # that ships to every user.
    for f in os.listdir(COMP):
        check(
            not f.lower().startswith("readme"),
            f"{f} sits inside the integration package; docs belong at the root",
        )

    # ---------------------------------------------------------- syntax
    for dirpath, _dirs, files in os.walk(COMP):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                try:
                    ast.parse(read(path))
                except SyntaxError as err:
                    failures.append(f"{f}: {err}")

    # ---------------------------------------------------------- report
    print(f"manifest {manifest.get('domain')} {manifest.get('version')}")
    for n in notes:
        print(f"  NOTE   {n}")
    for f in failures:
        print(f"  FAIL   {f}")
    if not failures:
        print("  all offline checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
