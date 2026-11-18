"""Hardening slice 6: the CI constraints snapshot covers every dependency and respects pyproject's ranges, and the
browser marker cannot skip silently in the browser job (docs/specs/hardening.md)."""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQ = re.compile(r"^([A-Za-z0-9_.\-]+)\s*((?:[<>=!~]=?\s*[0-9][0-9A-Za-z.*]*\s*,?\s*)*)$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:4])


def _pyproject_requirements() -> dict[str, list[tuple[str, str]]]:
    """name -> [(operator, version)] for `dependencies` and the dev/capture/ffmpeg extras (what CI installs)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found: list[str] = re.findall(r'^dependencies = \[(.*?)\]', text, re.M)
    for extra in ("dev", "capture", "ffmpeg"):
        found += re.findall(rf'^{extra} = \[(.*?)\]', text, re.M)
    out: dict[str, list[tuple[str, str]]] = {}
    for group in found:
        for req in re.findall(r'"([^"]+)"', group):
            m = REQ.match(req.strip())
            assert m, f"cannot read requirement {req!r}"
            out[_norm(m[1])] = re.findall(r"([<>=!~]=?)\s*([0-9][0-9A-Za-z.*]*)", m[2])
    return out


def _constraints() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / "ci" / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            assert version, f"constraints must pin exact versions: {line!r}"
            pins[_norm(name)] = version.strip()
    return pins


def _satisfies(version: str, op: str, bound: str) -> bool:
    v, b = _version(version), _version(bound)
    width = max(len(v), len(b))
    v, b = v + (0,) * (width - len(v)), b + (0,) * (width - len(b))
    return {">=": v >= b, ">": v > b, "<": v < b, "<=": v <= b, "==": v == b, "!=": v != b,
            "~=": v >= b and v[: len(_version(bound)) - 1] == b[: len(_version(bound)) - 1]}[op]


def test_every_ci_dependency_is_pinned_within_its_pyproject_range():
    reqs, pins = _pyproject_requirements(), _constraints()
    assert {"pillow", "numpy", "pyyaml", "pytest", "playwright", "imageio-ffmpeg"} <= set(reqs)
    for name, bounds in reqs.items():
        assert name in pins, f"{name} is a dependency but ci/constraints.txt does not pin it"
        for op, bound in bounds:
            assert _satisfies(pins[name], op, bound), f"{name}=={pins[name]} breaks {op}{bound} in pyproject.toml"


def test_the_range_check_itself():
    assert _satisfies("2.5.4", ">=", "1.24") and not _satisfies("17.0", "<", "17")
    assert _satisfies("1.4.2", "~=", "1.4") and not _satisfies("2.0", "~=", "1.4")


def test_the_workflow_runs_the_three_jobs_with_pinned_actions():
    wf = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for job in ("\n  test:", "\n  constraints:", "\n  browser:"):
        assert job in wf
    assert "-c ci/constraints.txt" in wf and "--upgrade-strategy eager" in wf
    assert 'YAMLREEL_REQUIRE_BROWSER: "1"' in wf and "-m browser" in wf and "playwright install --with-deps chromium" in wf
    assert wf.count("python -m yamlreel.diagnostics") == 3
    for use in re.findall(r"uses: (\S+)", wf):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), f"action not pinned to a commit: {use}"


STRICT_PROBE = textwrap.dedent('''
    import pytest

    @pytest.mark.browser
    def test_needs_a_browser():
        pytest.skip("no Chromium")

    def test_other_skips_are_untouched():
        pytest.skip("not about the browser")
''')


@pytest.mark.parametrize("strict,code", [("1", 1), ("", 0)])
def test_a_skipped_browser_test_fails_only_when_the_browser_is_required(tmp_path, strict, code):
    (tmp_path / "test_probe.py").write_text(STRICT_PROBE)
    (tmp_path / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    env = {"YAMLREEL_REQUIRE_BROWSER": strict, "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == code, done.stdout + done.stderr
    if strict:
        assert "1 failed, 1 skipped" in done.stdout and "YAMLREEL_REQUIRE_BROWSER=1" in done.stdout


def test_the_browser_tests_are_marked():
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only", "-m", "browser",
                           str(ROOT / "tests")], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    assert re.search(r"\b7/\d+ tests collected", done.stdout), done.stdout[-500:]
