"""Where in a YAML file an error is. `LocatedError` carries the file path and text a loader read; `line` and
`column` parse that text again only when someone asks, so a valid file never pays for it."""
from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import yaml

# Messages start `<path> ...` or `<path>: ...`. A message with no path (`spec needs ...`) starts with a word
# that is not a top-level key, so it either does not match or matches a name no mapping has.
_PATH = re.compile(r"^[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*")
_STEP = re.compile(r"\[(\d+)\]|\.?([A-Za-z_]\w*)")
_STR_TAG = "tag:yaml.org,2002:str"


def leading_path(msg: str) -> str | None:
    m = _PATH.match(msg)
    return m.group(0) if m else None


def locate(text: str, path: str, opaque: frozenset[str] = frozenset()) -> tuple[int, int] | None:
    """(line, column), 1-based, of `path` in `text`, or None. Follows only the nodes along the path, so an
    alias bomb is never expanded. A path that stops part way gives the deepest node reached, never the root."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        mark: yaml.Mark | None = None
        prefix = ""
        for m in _STEP.finditer(path):
            index, name = m.group(1), m.group(2)
            if index is not None and isinstance(node, yaml.SequenceNode) and int(index) < len(node.value):
                node = node.value[int(index)]
                mark = node.start_mark
                prefix += m.group(0)
            elif name is not None and isinstance(node, yaml.MappingNode):
                loader.flatten_mapping(node)   # PyYAML's own merge-key logic: explicit keys beat merged ones
                for key, value in reversed(node.value):   # the last duplicate wins, as in safe_load
                    if isinstance(key, yaml.ScalarNode) and key.tag == _STR_TAG and key.value == name:
                        node, mark = value, key.start_mark
                        break
                else:
                    break
                prefix += m.group(0)
            else:
                break
            if prefix in opaque:   # the config merges its own entries in first, so indices are not the file's
                break
        return (mark.line + 1, mark.column + 1) if mark else None
    except Exception:   # locating never hides the real error
        return None
    finally:
        loader.dispose()


def has_path(data: object, path: str) -> bool:
    """True if `path` (`a.b[1].c`) names an existing entry in loaded YAML data."""
    for m in _STEP.finditer(path):
        index, name = m.group(1), m.group(2)
        if index is not None and isinstance(data, list) and int(index) < len(data):
            data = data[int(index)]
        elif name is not None and isinstance(data, dict) and name in data:
            data = data[name]
        else:
            return False
    return True


class LocatedError(ValueError):
    """A ValueError that can say where in its source file it comes from."""
    OPAQUE: ClassVar[frozenset[str]] = frozenset()   # paths whose list indices are not the file's
    source: Path | str | None = None
    _text: str | None = None

    def attach(self, source: Path | str, text: str) -> None:
        self.source, self._text = source, text
        self.__dict__.pop("_where", None)

    @property
    def _loc(self) -> tuple[int, int] | None:
        if "_where" not in self.__dict__:
            try:
                path = leading_path(str(self)) if self._text else None
                self.__dict__["_where"] = locate(self._text or "", path, self.OPAQUE) if path else None
            except Exception:   # whatever goes wrong, the plain message is still right
                self.__dict__["_where"] = None
        return self.__dict__["_where"]

    @property
    def line(self) -> int | None:
        return self._loc[0] if self._loc else None

    @property
    def column(self) -> int | None:
        return self._loc[1] if self._loc else None
