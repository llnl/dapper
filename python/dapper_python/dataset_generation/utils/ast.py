from __future__ import annotations

import tree_sitter

from collections.abc import Generator


def ancestors(node: tree_sitter.Node) -> Generator[tree_sitter.Node, None, None]:
    """Yields all ancestor nodes of the provided node

    Order from closest to furthest ancestor
    parent -> grandparent -> ... -> root
    """
    current = node.parent
    while current:
        yield current
        current = current.parent


def descendants(node: tree_sitter.Node) -> Generator[tree_sitter.Node, None, None]:
    """Yields all descendant nodes of the provided node"""
    for child in node.children:
        yield child
        yield from descendants(child)
