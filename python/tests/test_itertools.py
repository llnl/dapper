import pytest

from dapper_python.utils.yet_more_itertools import chunked_iter, enforce_single_type


def test_chunked_iter():
    data = list(range(100))
    data_iter = (x for x in data)

    for i, chunk in enumerate(chunked_iter(data_iter, 10)):
        expected = list(range(i * 10, i * 10 + 10))
        assert list(chunk) == list(expected)

    data_iter = (x for x in data)
    for chunk in chunked_iter(data_iter, 23):
        list(chunk)

def test_enforce_single_type():
    data = [1, 2, 3, 4, 5]
    data_iter = (x for x in data)
    assert list(enforce_single_type(data_iter)) == data

    data = [1, 2, 3, 4.0, "5"]
    data_iter = (x for x in data)
    with pytest.raises(TypeError):
        assert list(enforce_single_type(data_iter)) == data