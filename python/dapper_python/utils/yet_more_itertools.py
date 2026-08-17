"""
Provides some additional iterator functionality not present in itertools or more_itertools

The modulename is a play on Python's itertools module, and another package more_itertools which adds additional iterator functionality
This adds even more functionality on top of those two
"""
from __future__ import annotations

import itertools
import more_itertools

from collections.abc import Iterable, Iterator, Generator
from typing import TypeVar
from typing import Optional

T = TypeVar("T")


def chunked_iter(iterable: Iterable[T], chunk_size: Optional[int]) -> Generator[Iterator[T], None, None]:
    """Splits an iterable into iterable chunks of size chunk_size

    Behaves very similarly to more_itertools.chunked, but is more memory efficient by operating only on iterators
    If run on a generator/iterator, it does not load all entries into memory the way that more_itertools.chunked does

    Instead of returning a list of up to N items, it returns a generator that itself yields up to those N items
    """
    # While this is fairly simple, it cannot be written directly into a comprehension, as comprehensions allow "for" loops but not "while"
    it = more_itertools.peekable(iterable)
    while it:
        # Let islice handle validation of batch_size argument
        yield itertools.islice(it, chunk_size)


def enforce_single_type(iterable: Iterable[T]) -> Generator[T, None, None]:
    """Ensures all objects in an iterable are of the same type without consuming the iterable to check

    Takes an iterable of objects and yields them back, behaving mostly transparently as if it were the original iterator
    However, if it encounters an object of a different type than ones that came before it, an error is raised

    Created as an inline way to ensure/enforce type matching without an operation like all(type(x) ... for x in iterable)
    Which would consume the iterable to check all types, thus leaving it unusable afterward
    """
    first, iterable = more_itertools.spy(iterable, n=1)
    if not first:
        return
    obj_type = type(first[0])

    for obj in iterable:
        if type(obj) is not obj_type:
            raise TypeError(f"Got different type {type(obj)}, expected {obj_type}")
        yield obj
