from __future__ import annotations

import requests
import time

from io import BytesIO
from http import HTTPStatus
from requests.exceptions import HTTPError, ConnectionError, Timeout
from tqdm.auto import tqdm

from collections.abc import Mapping
from typing import Union, Optional


def download(url: str, *,
             request_params: Optional[Mapping] = None,
             progress_params: Union[Mapping, bool] = False) -> tuple[BytesIO, Optional[str]]:
    """Utility function for downloading data with progress display

    :return: Tuple of (BytesIO, Content-Type) if content-type is not provided in response header, it will be None
    """
    if request_params is None:
        request_params = {}
    request_params.pop("stream", None)

    with requests.get(url, **request_params, stream=True) as web_request:
        web_request.raise_for_status()
        if 'content-length' in web_request.headers:
            file_size = int(web_request.headers['content-length'])
        else:
            file_size = None

        _progress_params = {
            "total": file_size,
            "desc": "Downloading file",
            "unit": 'B',
            "unit_divisor": 1024,
            "unit_scale": True,
            "position": None,
            "leave": None,
        }
        if isinstance(progress_params, Mapping):
            _progress_params.update(progress_params)
        elif isinstance(progress_params, bool):
            _progress_params["disable"] = not progress_params

        content = BytesIO()
        with tqdm(**_progress_params) as progress_bar:
            for chunk in web_request.iter_content(chunk_size=8 * 1024):
                content.write(chunk)
                progress_bar.update(len(chunk))

        content.seek(0)
        return content, web_request.headers.get('Content-Type', None)


def get_with_retry(url: str, *, retries: int = 5, **kwargs) -> requests.Response:
    """Wrapper around requests.get with support for automatic retries on failure

    Attempts to retrieve the web content from the specified URL
    Will retry if the request is not successful (i.e does not receive an HTTP OK status)
    """
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, **kwargs)
            response.raise_for_status()
            return response

        except (ConnectionError, Timeout):
            if attempt >= retries:
                raise
            time.sleep(1)

        except HTTPError as e:
            if attempt >= retries:
                raise

            match e.response.status_code:
                case HTTPStatus.NOT_FOUND:
                    raise
                case HTTPStatus.TOO_MANY_REQUESTS:
                    retry_after = e.response.headers.get('Retry-After', 1)
                    time.sleep(retry_after)
                case _:
                    time.sleep(1)
    else:
        # We should never reach this. All paths should either return or raise
        # Only present for static type checking
        raise RuntimeError
