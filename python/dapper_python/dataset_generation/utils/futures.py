from __future__ import annotations

import math
import itertools
import concurrent.futures

from concurrent.futures import Future, Executor
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from concurrent.futures import FIRST_COMPLETED

from collections.abc import Iterable, Callable, Generator
from typing import TypeVar, Type
from typing import ClassVar
from typing import Union, Optional

T = TypeVar("T")
U = TypeVar("U")
R = TypeVar("R")
ExceptionGroupType = Union[Type[Exception], tuple[Type[Exception], ...]]


class BoundedSubmissionMixin(Executor):
    """Mixin class for concurrent.futures executor pools to allow bounded submission of tasks"""

    def bounded_run(self, _iter: Iterable[Callable[[], R]], *,
                    bound: Optional[int] = None) -> Generator[Future[R], None, None]:
        """Submits tasks to the pool with a bound on the number of tasks submitted at any given time
        Yields future objects as they complete

        Allows for processing a large number of tasks without upfront initialization of all futures in memory
        Creating from generator can mean only a handful of futures are created at a time

        Equivalent functionality exists in python 3.14+ using Executor.map() with buffersize provided
        However, this does not exist in older versions; hence this implementation
        """
        if bound is None:
            pool_size = getattr(self, "_pool_size", self._FALLBACK_SUBMISSION_WORKERS)
            bound = int(math.ceil(pool_size * self.SUBMISSION_BOUND_RATIO))

        futures = set()
        it = iter(_iter)

        for _callable in itertools.islice(it, bound):
            futures.add(self.submit(_callable))

        while futures:
            done, futures = concurrent.futures.wait(futures, return_when=FIRST_COMPLETED)
            for _callable in itertools.islice(it, len(done)):
                futures.add(self.submit(_callable))
            yield from done

    # Ratio of number of tasks to submit compared to the number of workers
    # If there are 6 workers and the ratio is 2, then 12 tasks will be in the queue at any given time
    SUBMISSION_BOUND_RATIO: ClassVar[float] = 2
    # Number of assumed workers if unable to determine the pool's worker count
    _FALLBACK_SUBMISSION_WORKERS: ClassVar[int] = 4


class BoundedThreadPoolExecutor(ThreadPoolExecutor, BoundedSubmissionMixin):
    ...


class BoundedProcessPoolExecutor(ProcessPoolExecutor, BoundedSubmissionMixin):
    ...


def result_or_default(future: Future[T], *, suppress: ExceptionGroupType, default: U) -> Union[T, U]:
    """Gets the result from a future object, returning the default if a provided exception is raised
    Intended for use in generator/list comprehensions since try/except blocks are not easily used in such comprehensions

    If the exception is not in the supress list, then the exception will be raised
    """
    if isinstance(suppress, type) and issubclass(suppress, Exception):
        suppress = (suppress,)

    try:
        return future.result()
    except suppress:
        return default
