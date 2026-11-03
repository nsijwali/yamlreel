"""The `tree` scene: validation, the pure layout and smoke renders in both orientations."""
from __future__ import annotations

import pytest

from yamlreel.datascenes import MAX_TREE_CHILDREN, MAX_TREE_LEVELS, MAX_TREE_NODES, TreeScene, tree_layout
from yamlreel.render import Timeline
from yamlreel.specs import SpecError, validate

from test_validation import _literal_keys

NODES = [{"id": "r", "label": "M"}, {"id": "a", "label": "D H", "parent": "r"},
         {"id": "b", "label": "Q T", "parent": "r", "at": 1.2}, {"id": "c", "label": "A B", "parent": "a"},
         {"id": "d", "label": "E F", "parent": "a"}]
TREE = {"type": "tree", "title": "B-tree", "seconds": 8, "nodes": NODES,
        "path": [{"at": 3, "nodes": ["r", "a", "c"], "text": "2 hops"}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)


def check(**kw):
    return validate({"scenes": [{**TREE, **kw}]})["scenes"][0]


def chain(n):
    return [{"id": f"n{i}", "label": str(i), **({"parent": f"n{i - 1}"} if i else {})} for i in range(n)]


@pytest.mark.parametrize("kw,msg", [
    ({"nodes": []}, "scenes[0].nodes must have at least 1 entry"),
    ({"nodes": [{"id": "r"}]}, "scenes[0].nodes[0]: every node needs id and label"),
    ({"nodes": [{"id": "r", "label": "x", "parent": "q"}]}, "scenes[0].nodes[0].parent: the first node is the root"),
    ({"nodes": [NODES[0], {"id": "a", "label": "A"}]}, "scenes[0].nodes[1]: needs parent"),
    ({"nodes": [NODES[0], {"id": "r", "label": "x", "parent": "r"}]}, "scenes[0].nodes[1].id: duplicate node id 'r'"),
    ({"nodes": [NODES[0], {"id": "a", "label": "x", "parent": "z"}]}, "scenes[0].nodes[1].parent: unknown node 'z'"),
    ({"nodes": [{"id": "a", "label": "x", "parent": None}, {"id": "b", "label": "x", "parent": "c"},
                {"id": "c", "label": "x", "parent": "a"}]}, "scenes[0].nodes[1].parent: unknown node 'c'"),
    ({"nodes": [NODES[0], {"id": "a", "label": "x", "parent": "r", "color": 1}]}, "scenes[0].nodes[1]"),
    ({"nodes": [NODES[0], {"id": "a", "label": "x", "parent": "r", "at": -1}]}, "scenes[0].nodes[1].at must be a number"),
    ({"nodes": chain(MAX_TREE_NODES + 1)}, "scenes[0].nodes has 32 entries; the limit is 31"),
    ({"nodes": chain(MAX_TREE_LEVELS + 1)}, "scenes[0].nodes[5].parent: this would be level 6"),
    ({"nodes": [NODES[0], *({"id": f"c{i}", "label": "x", "parent": "r"} for i in range(MAX_TREE_CHILDREN + 1))]},
     f"scenes[0].nodes[{MAX_TREE_CHILDREN + 1}].parent: 'r' already has 6 children"),
    ({"path": [{"at": 1}]}, "scenes[0].path[0]: needs nodes"),
    ({"path": [{"nodes": ["r"]}]}, "scenes[0].path[0]: needs at"),
    ({"path": [{"at": 1, "nodes": ["r", "zz"]}]}, "scenes[0].path[0].nodes[1]: unknown node 'zz'"),
    ({"path": [{"at": 1, "nodes": ["r", "c"]}]}, "scenes[0].path[0].nodes[1]: 'c' is not next to 'r'"),
    ({"path": [{"at": 1, "nodes": []}]}, "scenes[0].path[0].nodes must have at least 1 entry"),
    ({"path": [{"at": 2, "nodes": ["r"]}, {"at": 1, "nodes": ["r"]}]}, "scenes[0].path[1].at must not be earlier"),
    ({"path": [{"at": 1, "nodes": ["r"], "color": "red"}]}, "scenes[0].path[0]"),
    ({"path": "r"}, "scenes[0].path must be a list"),
    ({"title": ["x"]}, "scenes[0].title must be text"),
])
def test_bad_trees_are_rejected(kw, msg):
    with pytest.raises(SpecError) as e:
        check(**kw)
    assert msg in str(e.value)


def test_nodes_are_required_and_limits_are_inclusive():
    with pytest.raises(SpecError, match="missing nodes"):
        validate({"scenes": [{"type": "tree"}]})
    assert validate({"scenes": [{"type": "tree", "nodes": NODES}]})["scenes"][0]["seconds"] == 8.0
    assert len(check(nodes=chain(MAX_TREE_LEVELS), path=[])["nodes"]) == MAX_TREE_LEVELS
    wide = [NODES[0], *({"id": f"c{i}", "label": "x", "parent": "r"} for i in range(MAX_TREE_CHILDREN))]
    assert len(check(nodes=wide, path=[])["nodes"]) == 7
    assert check(nodes=[{"id": 1, "label": 5}], path=[{"at": 0, "nodes": [1]}])["path"][0]["nodes"] == ["1"]


def test_a_path_may_walk_up_as_well_as_down():
    assert check(path=[{"at": 1, "nodes": ["c", "a", "r", "b"]}])["path"][0]["nodes"] == ["c", "a", "r", "b"]


def test_layout_centres_parents_over_leaf_slots():
    pos = tree_layout(check()["nodes"])
    assert pos["c"][0] == pytest.approx(1 / 6) and pos["d"][0] == pytest.approx(3 / 6) and pos["b"][0] == pytest.approx(5 / 6)
    assert pos["a"][0] == pytest.approx((pos["c"][0] + pos["d"][0]) / 2)
    assert pos["r"][0] == pytest.approx((pos["a"][0] + pos["b"][0]) / 2)
    assert pos["r"][1] == 0.0 and pos["a"][1] == pytest.approx(0.5) and pos["c"][1] == 1.0
    assert all(0 <= x <= 1 and 0 <= y <= 1 for x, y in pos.values())


def test_layout_edge_cases():
    assert tree_layout([]) == {}
    assert tree_layout([{"id": "r", "label": "x"}]) == {"r": (0.5, 0.0)}
    pos = tree_layout(chain(4))   # a chain stays on one vertical line
    assert {round(x, 6) for x, _ in pos.values()} == {0.5}
    assert [pos[f"n{i}"][1] for i in range(4)] ==pytest.approx([0, 1 / 3, 2 / 3, 1])


def test_layout_keeps_same_level_nodes_one_slot_apart():
    nodes = [{"id": "r", "label": "x"}, {"id": "a", "label": "x", "parent": "r"}, {"id": "b", "label": "x", "parent": "r"},
             {"id": "a1", "label": "x", "parent": "a"}, {"id": "a2", "label": "x", "parent": "a"},
             {"id": "a3", "label": "x", "parent": "a"}, {"id": "b1", "label": "x", "parent": "b"}]
    pos = tree_layout(nodes)
    slot = 1 / 4
    for level in (1, 2):
        xs = sorted(x for i, (x, y) in pos.items() if y == pytest.approx(level / 2))
        assert all(b - a >= slot - 1e-9 for a, b in zip(xs, xs[1:]))


def test_tree_reads_only_declared_fields():
    read = _literal_keys(TreeScene.__init__, {"spec"})
    assert read and read <= TreeScene.FIELDS
    assert _literal_keys(TreeScene.__init__, {"p"}) <= TreeScene.PATH_FIELDS
    assert _literal_keys(TreeScene.__init__, {"n"}) <= TreeScene.NODE_FIELDS


def scene(tmp_path, fmt="landscape", scale=0.5, **kw) -> TreeScene:
    tl = Timeline(validate({"scenes": [{**TREE, **kw}]}), fmt, tmp_path, scale=scale)
    sc = tl.scenes[0]
    assert isinstance(sc, TreeScene)
    return sc


@pytest.mark.parametrize("fmt", ["landscape", "vertical"])
def test_renders_nodes_then_path(tmp_path, fmt):
    sc = scene(tmp_path, fmt)
    frames = [sc.frame(t) for t in (0.1, 2.5, 3.4, 6.0)]   # nothing yet, all nodes, mid path, path done
    assert all(f.size == (sc.ctx.W, sc.ctx.H) for f in frames)
    assert len({f.tobytes() for f in frames}) == 4
    assert sc.ctx.warnings == []


def test_boxes_stay_inside_the_frame_and_use_the_vertical_width(tmp_path):
    sc = scene(tmp_path, "vertical", nodes=chain(1) + [{"id": f"c{i}", "label": "k", "parent": "n0"} for i in range(6)],
               path=[])
    boxes = [sc.box(k) for k in range(len(sc.ids))]
    assert all(b[0] >= 0 and b[2] <= sc.ctx.W and b[1] >= 0 and b[3] <= sc.ctx.H * 0.72 for b in boxes)
    leaves = boxes[1:]
    assert all(a[2] <= b[0] for a, b in zip(leaves, leaves[1:]))   # no overlap
    assert leaves[-1][2] - leaves[0][0] >= sc.ctx.W * 0.8          # the row spans most of the width


def test_a_full_tree_renders_without_overlap(tmp_path):
    nodes = [{"id": "r", "label": "root"}]
    for i in range(5):
        nodes.append({"id": f"m{i}", "label": f"m{i}", "parent": "r"})
        for j in range(5):
            nodes.append({"id": f"m{i}{j}", "label": f"{i}{j}", "parent": f"m{i}"})
    assert len(nodes) == 31
    sc = scene(tmp_path, "vertical", scale=1.0, nodes=nodes, path=[{"at": 4, "nodes": ["r", "m2", "m23"]}], seconds=8)
    boxes = sorted((sc.box(k) for k in range(31) if sc.pos[k][1] == sc.pos[-1][1]), key=lambda b: b[0])
    assert all(a[2] <= b[0] for a, b in zip(boxes, boxes[1:]))
    assert sc.frame(7.0).size == (1080, 1920)


def test_lights_follow_the_path_hop_by_hop(tmp_path):
    sc = scene(tmp_path)
    ids = {n: k for k, n in enumerate(sc.ids)}
    nodes, edges = sc.lights(2.9)
    assert not any(nodes) and not edges
    nodes, edges = sc.lights(3.0 + 0.35 * 0.5)       # halfway through hop 1: root lit, edge r->a half drawn
    assert nodes[ids["r"]] > 0.9 and nodes[ids["a"]] == 0
    assert edges[ids["a"]][0] == pytest.approx(0.5) and edges[ids["a"]][1] == ids["r"]
    nodes, edges = sc.lights(3.0 + 0.7 + 0.5)        # all hops done
    assert nodes[ids["c"]] > 0.9 and edges[ids["c"]][0] == 1.0
    assert nodes[ids["b"]] == 0


def test_an_earlier_path_dims_when_the_next_starts(tmp_path):
    sc = scene(tmp_path, path=[{"at": 1, "nodes": ["r", "a"]}, {"at": 4, "nodes": ["r", "b"]}])
    ids = {n: k for k, n in enumerate(sc.ids)}
    assert sc.lights(3.9)[0][ids["a"]] == 1.0
    assert sc.lights(4.6)[0][ids["a"]] == 0.0 and sc.lights(4.6)[0][ids["b"]] > 0.9


def test_nodes_without_at_are_staggered(tmp_path):
    sc = scene(tmp_path)
    assert sc.appear_at[0] == pytest.approx(0.3) and sc.appear_at[1] == pytest.approx(0.42)
    assert sc.appear_at[2] == 1.2                         # explicit `at`
    assert sc.appear_at[3] == pytest.approx(0.54)         # the stagger counts only nodes without `at`


def test_warns_when_a_step_ends_after_the_scene(tmp_path):
    tl = Timeline(validate({"scenes": [{**TREE, "seconds": 3.3}]}), "landscape", tmp_path, scale=0.5)
    assert any("tree node or path step ends after the scene" in w for w in tl.ctx.warnings)
