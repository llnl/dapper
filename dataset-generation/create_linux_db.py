from __future__ import annotations

import argparse
import requests
import gzip

from enum import Enum
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from datetime import datetime
from io import BytesIO, TextIOWrapper
from http import HTTPStatus
from debian.deb822 import Deb822, Sources
from tqdm.auto import tqdm

from typing import ClassVar
from collections.abc import Mapping, Sequence

from dapper_python.databases_v2.database import Metadata
from dapper_python.databases_v2.linux_db import LinuxDatabase, PackageFile, PackageSource


class LinuxDistro:
    class Distro(Enum):
        """Currently supported distros"""
        UBUNTU = 'ubuntu'
        DEBIAN = 'debian'

    @dataclass
    class _DistroInfo:
        archive_url: str
        contents_path: str
        sources_path: str

        @property
        def contents_url(self) -> str:
            return self.archive_url + self.contents_path

        @property
        def sources_url(self) -> str:
            return self.archive_url + self.sources_path

    def __init__(self, distro: Distro, release: str) -> None:
        try:
            candidate_infos = self.DISTRO_MAP[distro]
        except KeyError as e:
            raise KeyError(f"Invalid distro: {distro}") from e

        # Check if the release actually exists, if we get a non-404 then it means it likely exists
        if not isinstance(candidate_infos, Sequence):
            candidate_infos = (candidate_infos,)
        for candidate_info in candidate_infos:
            with requests.head(candidate_info.contents_url.format(release=release)) as response:
                if response.status_code != HTTPStatus.NOT_FOUND:
                    self._dist_info = candidate_info
                    break
        else:  # Exits loop without break
            raise ValueError(f"Release {release} does not exist for distro \"{distro.value}\"")

        self._distro = distro
        self._release = release

    def get_contents(self, **kwargs) -> TextIOWrapper:
        """Downloads the contents file for the distro + release"""
        data, _ = self.get_file(self._dist_info.contents_path.format(release=self._release), **kwargs)
        with gzip.open(data) as gz_file:
            return TextIOWrapper(BytesIO(gz_file.read()), encoding="utf-8")

    def get_sources(self, **kwargs) -> TextIOWrapper:
        """Downloads the sources file for the distro + release"""
        data, _ = self.get_file(self._dist_info.sources_path.format(release=self._release), **kwargs)
        with gzip.open(data) as gz_file:
            return TextIOWrapper(BytesIO(gz_file.read()), encoding="utf-8")

    def get_file(self, path: str, *, progress_params: Mapping | bool = False) -> tuple[BytesIO, str | None]:
        """Utility function for downloading files from the distro archive"""
        url = self._dist_info.archive_url + path
        with requests.get(url, stream=True) as response:
            response.raise_for_status()
            if 'content-length' in response.headers:
                file_size = int(response.headers['content-length'])
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
                for chunk in response.iter_content(chunk_size=8 * 1024):
                    content.write(chunk)
                    progress_bar.update(len(chunk))

            content.seek(0)
            return content, response.headers.get('Content-Type', None)

    DISTRO_MAP: ClassVar[dict[Distro, _DistroInfo]] = {
        Distro.UBUNTU: _DistroInfo(
            archive_url=r"https://archive.ubuntu.com/ubuntu/",
            contents_path=r"dists/{release}/Contents-amd64.gz",
            sources_path=r"dists/{release}/main/source/Sources.gz",
        ),
        Distro.DEBIAN: (
            # Debian has two different sites for currently supported distros and older archived distros
            # Need to check both
            _DistroInfo(
                archive_url=r"https://deb.debian.org/debian/",
                contents_path=r"dists/{release}/main/Contents-amd64.gz",
                sources_path=r"dists/{release}/main/source/Sources.gz",
            ),
            _DistroInfo(
                archive_url=r"https://archive.debian.org/debian/",
                contents_path=r"dists/{release}/main/Contents-amd64.gz",
                sources_path=r"dists/{release}/main/source/Sources.gz",
            ),
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Create Linux DB by parsing the Linux Contents file",
    )
    parser.add_argument(
        "-o", "--output",
        required=False,
        type=Path, default=Path('LinuxPackageDB.db'),
        help='Path of output (database) file to create. Defaults to "LinuxPackageDB.db" in the current working directory',
    )
    parser.add_argument(
        '-v', '--version',
        type=int, required=True,
        help='Version marker for the database to keep track of changes',
    )

    parser.add_argument(
        "distro",
        type=LinuxDistro.Distro, choices=LinuxDistro.Distro,
        help="Name of the distro to scrape",
    )
    parser.add_argument(
        "release",
        type=str,
        help="Name of the release to scrape",
    )
    args = parser.parse_args()

    # Currently not set up to be able to handle resuming a previously started database
    # It's not a high priority as the process only takes few minutes to process. Can delete the old DB and recreate
    if args.output.exists():
        raise FileExistsError(f"File {args.output} already exists")

    linux_distro = LinuxDistro(args.distro, args.release)

    linux_db = LinuxDatabase.create_database(args.output, exist_ok=False)
    with linux_db.session() as session:
        # Parse contents file
        with session.begin():
            contents_data = linux_distro.get_contents(progress_params=True)
            entry_count = sum(1 for _ in contents_data)
            contents_data.seek(0)

            # Operate using generator expressions for more efficient memory usage
            progress_iter = tqdm(
                contents_data,
                total=entry_count,
                desc='Processing Contents', colour='green',
                unit='Entry',
            )
            parsed_lines = (
                tuple(x.strip() for x in entry.rsplit(maxsplit=1))
                for entry in progress_iter
            )
            package_files = (
                PackageFile(
                    file_path=PurePosixPath(file_path),
                    package_name=full_package_name.rsplit('/', maxsplit=1)[-1],
                    full_package_name=full_package_name,
                )
                for file_path, full_package_name in parsed_lines
            )
            session.bulk_insert(package_files, batch_size=50_000)

        # Parse sources file
        with session.begin():
            sources_data = linux_distro.get_sources(progress_params=True)
            entry_count = sum(1 for _ in Deb822.iter_paragraphs(sources_data))
            sources_data.seek(0)

            # Operate using generator expressions for more efficient memory usage
            progress_iter = tqdm(
                Deb822.iter_paragraphs(sources_data),
                total=entry_count,
                desc='Processing Sources', colour='cyan',
                unit='Entry',
            )
            package_sources = (
                PackageSource(
                    package_name=entry.get("Package"),
                    bin_package=bin_package.strip(),
                )
                for entry in progress_iter
                for bin_package in entry.get('Binary').split(',')
            )
            session.bulk_insert(package_sources, batch_size=50_000)

        # Set version
        with session.begin():
            session.add(Metadata(
                version=args.version,
                format="Linux",
                timestamp=int(datetime.now().timestamp()),
            ))


if __name__ == "__main__":
    main()
