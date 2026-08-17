import pytest

import functools

from dapper_python.dataset_generation.utils.futures import BoundedThreadPoolExecutor


def test_bounded_executor():
    # Don't really have a good way to examine the backend as to what has been submitted when,
    # but can at least make sure we get the intended results back out
    def example_func(num: int) -> int:
        return num * 2

    with BoundedThreadPoolExecutor() as pool:
        threads = ((
            functools.partial(example_func, x)
            for x in range(1000)
        ))
        results = pool.bounded_run(threads)

        results = sorted([x.result() for x in results])

    expected = list(range(0, 2000, 2))
    assert results == expected
