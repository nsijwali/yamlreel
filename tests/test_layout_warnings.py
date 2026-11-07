"""Layout warnings (array labels, diagram overlaps) and the voice server's 503 retry."""
from __future__ import annotations

from urllib.error import HTTPError, URLError

import pytest

from yamlreel import voice as voice_mod
from yamlreel.config import Config
from yamlreel.render import Timeline
from yamlreel.specs import validate
from yamlreel.voice import VoiceError, VoiceServer


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
        monkeypatch.delenv(f"YAMLREEL_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def timeline(tmp_path, scenes, fmt="landscape") -> Timeline:
    spec = validate({"scenes": scenes, "music": {"style": "none"}}, Config())
    return Timeline(spec, fmt, tmp_path, scale=0.5)


TEN = [str(i) for i in range(10)]


def array(**kw):
    return {"type": "array", "items": TEN, "seconds": 8, **kw}


# --- array labels -----------------------------------------------------------------------------------------

def test_array_warns_when_a_step_label_runs_off_the_left_or_right_edge(tmp_path):
    left = timeline(tmp_path, [array(steps=[{"at": 1, "mark": [0], "text": "zero: never added"}])], "vertical")
    right = timeline(tmp_path, [array(steps=[{"at": 1, "mark": [9], "text": "last one, far right"}])], "vertical")
    both = timeline(tmp_path, [array(steps=[{"at": 1, "window": [0, 9], "text": "M" * 40}])], "vertical")
    assert [w for w in left.warnings if "clipped" in w] == [
        "scenes[0]: array label 'zero: never added' is clipped at the left edge of the frame; "
        "put it on a cell nearer the middle or shorten it"]
    assert any("'last one, far right' is clipped at the right edge" in w for w in right.warnings)
    assert any("clipped at the left and right edge" in w for w in both.warnings)


def test_array_label_warning_names_its_scene_index(tmp_path):
    tl = timeline(tmp_path, [{"type": "title", "title": "x", "seconds": 3},
                             array(steps=[{"at": 1, "mark": [0], "text": "zero: never added"}])], "vertical")
    assert any(w.startswith("scenes[1]: array label") for w in tl.warnings)


def test_array_labels_that_fit_do_not_warn(tmp_path):
    tl = timeline(tmp_path, [{"type": "array", "items": ["a", "b", "c"], "seconds": 8,
                              "steps": [{"at": 1, "mark": [1], "text": "mid"}]}])
    assert not any("clipped" in w for w in tl.warnings)


# --- diagram overlaps -------------------------------------------------------------------------------------

def diagram(*extra):
    return {"type": "diagram", "title": "A diagram title", "caption": "What happens next", "seconds": 5,
            "nodes": [{"id": "a", "label": "Client", "x": .3, "y": .5}, {"id": "b", "label": "Server", "x": .7, "y": .5},
                      *extra]}


def test_diagram_without_overlaps_is_quiet(tmp_path):
    for fmt in ("landscape", "vertical", "square"):
        assert not any("overlaps" in w for w in timeline(tmp_path, [diagram()], fmt).warnings)


def test_diagram_warns_when_the_caption_or_title_covers_a_node(tmp_path):
    low = {"id": "low", "label": "Cache", "x": .5, "y": .5, "vy": .76}
    top = {"id": "top", "label": "Gate", "x": .5, "y": .5, "vy": .09}
    tl = timeline(tmp_path, [diagram(low, top)], "vertical")
    assert any(w.startswith("scenes[0]: the caption overlaps diagram node 'low'") for w in tl.warnings)
    assert any(w.startswith("scenes[0]: the title overlaps diagram node 'top'") for w in tl.warnings)
    assert not any("node 'a'" in w or "node 'b'" in w for w in tl.warnings)


def test_diagram_caption_overlap_needs_a_caption(tmp_path):
    spec = diagram({"id": "low", "label": "Cache", "x": .5, "y": .5, "vy": .76})
    del spec["caption"]
    assert not any("caption" in w for w in timeline(tmp_path, [spec], "vertical").warnings)


# --- voice server still loading ---------------------------------------------------------------------------

PAYLOAD = {"jsonrpc": "2.0", "id": 1, "method": "x"}


def http_error(code: int) -> HTTPError:
    return HTTPError("http://voice.example/mcp/", code, "Service Unavailable" if code == 503 else "Error", {}, None)  # type: ignore[arg-type]


class Reply:
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return b'{"id": 1, "result": {}}'


def session(monkeypatch, outcomes):
    """A voice session whose urlopen gives the outcomes in turn (the last repeats); returns it, the sleeps, the calls."""
    sleeps, calls = [], []

    def fake_urlopen(req, timeout):
        calls.append(req)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return Reply()

    monkeypatch.setattr(voice_mod, "urlopen", fake_urlopen)
    monkeypatch.setattr(voice_mod.time, "sleep", sleeps.append)
    return voice_mod._Session(VoiceServer(url="http://voice.example/mcp/")), sleeps, calls


def test_a_503_is_retried_with_growing_waits_until_the_model_has_loaded(monkeypatch):
    s, sleeps, calls = session(monkeypatch, [http_error(503), http_error(503), None])
    assert s._post(PAYLOAD) == {"id": 1, "result": {}}
    assert len(calls) == 3 and sleeps == [3.0, 4.5]


def test_a_503_every_time_keeps_the_error_text_and_says_the_server_may_be_loading(monkeypatch):
    s, sleeps, calls = session(monkeypatch, [http_error(503)])
    with pytest.raises(VoiceError, match=r"cannot reach the voice server at http://voice.example/mcp/: "
                                         r"Service Unavailable\. Is VoiceStudio running\?.*may still be loading"):
        s._post(PAYLOAD)
    assert len(calls) == voice_mod.LOADING_TRIES and len(sleeps) == voice_mod.LOADING_TRIES - 1


@pytest.mark.parametrize("err", [http_error(500), URLError("refused")])
def test_other_failures_are_not_retried_and_do_not_mention_loading(monkeypatch, err):
    s, sleeps, calls = session(monkeypatch, [err])
    with pytest.raises(VoiceError, match="cannot reach the voice server") as ei:
        s._post(PAYLOAD)
    assert len(calls) == 1 and not sleeps and "loading" not in str(ei.value)
