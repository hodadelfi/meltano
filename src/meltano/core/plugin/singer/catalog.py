import logging
import re
from fnmatch import fnmatch
from collections import OrderedDict, namedtuple
from enum import Enum, auto
from functools import singledispatch
from typing import List


SelectPattern = namedtuple(
    "SelectPattern", ("stream_pattern", "property_pattern", "negated")
)


def parse_select_pattern(pattern: str):
    negated = False

    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]

    stream, *_ = pattern.split(".")

    return SelectPattern(
        stream_pattern=stream, property_pattern=pattern, negated=negated
    )


class CatalogNode(Enum):
    STREAM = auto()
    STREAM_METADATA = auto()
    STREAM_PROPERTY = auto()
    STREAM_PROPERTY_METADATA = auto()


class CatalogExecutor:
    def execute(self, node_type: CatalogNode, node, path):
        dispatch = {
            CatalogNode.STREAM: self.stream_node,
            CatalogNode.STREAM_METADATA: self.stream_metadata_node,
            CatalogNode.STREAM_PROPERTY_METADATA: self.property_metadata_node,
            CatalogNode.STREAM_PROPERTY: self.property_node,
        }

        try:
            dispatch[node_type](node, path)
        except KeyError:
            logging.debug(f"Unknown node type '{node_type}'.")

    def stream_node(self, node, path: str):
        pass

    def property_node(self, node, path: str):
        pass

    def stream_metadata_node(self, node, path: str):
        pass

    def property_metadata_node(self, node, path: str):
        pass

    def __call__(self, node_type, node, path):
        return self.execute(node_type, node, path)


class SelectExecutor(CatalogExecutor):
    def __init__(self, patterns: List[str]):
        self._stream = None
        self._patterns = list(map(parse_select_pattern, patterns))

    @property
    def current_stream(self):
        return self._stream["stream"]

    @classmethod
    def _match_patterns(cls, value, include=[], exclude=[]):
        included = any(fnmatch(value, pattern) for pattern in include)
        excluded = any(fnmatch(value, pattern) for pattern in exclude)

        return included and not excluded

    def update_node_selection(self, node, path: str, selected: bool):
        node["selected"] = selected
        if selected:
            logging.debug(f"{path} has been selected.")
        else:
            logging.debug(f"{path} has not been selected.")

    def stream_match_patterns(self, stream):
        return self._match_patterns(
            stream,
            include=(
                pattern.stream_pattern
                for pattern in self._patterns
                if not pattern.negated
            ),
        )

    def property_match_patterns(self, prop):
        return self._match_patterns(
            prop,
            include=(
                pattern.property_pattern
                for pattern in self._patterns
                if not pattern.negated
            ),
            exclude=(
                pattern.property_pattern
                for pattern in self._patterns
                if pattern.negated
            ),
        )

    def stream_node(self, node, path):
        self._stream = node
        selected = self.stream_match_patterns(self.current_stream)
        stream_metadata = {
            "breadcrumb": [],
            "metadata": {"inclusion": "automatic"},
        }

        try:
            metadata = next(
                metadata
                for metadata in node["metadata"]
                if len(metadata["breadcrumb"]) == 0
            )
            self.update_node_selection(metadata["metadata"], path, selected)
        except KeyError:
            node["metadata"] = [stream_metadata]
        except StopIteration:
            # This is to support legacy catalogs
            node["metadata"].insert(0, stream_metadata)

        # the node itself has a `selected` key
        self.update_node_selection(node, path, selected)

    def stream_metadata_node(self, node, path):
        metadata = node["metadata"]
        selected = self.stream_match_patterns(self.current_stream)
        self.update_node_selection(metadata, path, selected)

    def property_node(self, node, path):
        prop_regex = r"properties\.(\w+)+"
        components = re.findall(prop_regex, path)
        breadcrumb = [self.current_stream, *components]

        try:
            next(
                metadata
                for metadata in self._stream["metadata"]
                if metadata["breadcrumb"] == breadcrumb
            )
        except StopIteration:
            # This is to support legacy catalogs
            self._stream["metadata"].append(
                {
                    "breadcrumb": breadcrumb,
                    "metadata": {"inclusion": "automatic"},
                },
            )

    def property_metadata_node(self, node, path):
        prop = ".".join(node["breadcrumb"])
        selected = self.property_match_patterns(prop)
        self.update_node_selection(node["metadata"], path, selected)


class ListExecutor(CatalogExecutor):
    def __init__(self, selected_only=False):
        # properties per stream
        self.properties = OrderedDict()

        super().__init__()

    def stream_node(self, node, path):
        stream = node["stream"]
        if stream not in self.properties:
            self.properties[stream] = set()

    def property_node(self, node, path):
        *_, name = path.split(".")
        # current stream
        stream = next(reversed(self.properties))
        self.properties[stream].add(name)


class ListSelectedExecutor(CatalogExecutor):
    SelectedNode = namedtuple("SelectedNode", ("key", "selected"))

    def __init__(self):
        self.streams = set()
        self.properties = OrderedDict()
        super().__init__()

    @property
    def selected_properties(self):
        # we don't want to mutate the visitor result
        selected = self.properties.copy()

        # remove all non-selected streams
        for stream in (name for name, selected in self.streams if not selected):
            del selected[stream]

        # remove all non-selected properties
        for stream, props in selected.items():
            selected[stream] = {name for name, selected in props if selected}

        return selected

    def is_node_selected(self, node):
        try:
            metadata = node["metadata"]
            return metadata.get("inclusion") == "automatic" or metadata.get(
                "selected", False
            )
        except KeyError:
            return False

    def stream_node(self, node, path):
        self._stream = node["stream"]
        self.properties[self._stream] = set()

    def stream_metadata_node(self, node, path):
        selection = self.SelectedNode(self._stream, self.is_node_selected(node))
        self.streams.add(selection)

    def property_metadata_node(self, node, path):
        name = ".".join(node["breadcrumb"][1:])
        selection = self.SelectedNode(name, self.is_node_selected(node))

        self.properties[self._stream].add(selection)


@singledispatch
def visit(node, executor, path: str = ""):
    logging.debug(f"Skipping node at '{path}'")


@visit.register(dict)
def _(node: dict, executor, path=""):
    logging.debug(f"Visiting node at '{path}'.")
    if re.search(r"streams\[\d+\]$", path):
        executor(CatalogNode.STREAM, node, path)

    if re.search(r"schema\.properties\..*$", path):
        executor(CatalogNode.STREAM_PROPERTY, node, path)

    if re.search(r"metadata\[\d+\]$", path) and "breadcrumb" in node:
        if len(node["breadcrumb"]) == 0:
            executor(CatalogNode.STREAM_METADATA, node, path)
        else:
            executor(CatalogNode.STREAM_PROPERTY_METADATA, node, path)

    for child_path, child_node in node.items():
        visit(child_node, executor, path=f"{path}.{child_path}")


@visit.register(list)
def _(node: list, executor, path=""):
    for index, child_node in enumerate(node):
        visit(child_node, executor, path=f"{path}[{index}]")
