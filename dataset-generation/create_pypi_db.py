from __future__ import annotations

import argparse
import requests
import zipfile, zlib
import functools
import methodtools
import more_itertools
import magic

from pathlib import Path, PurePosixPath
from dataclasses import dataclass
from sqlmodel import select, delete
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile
from contextlib import suppress, ExitStack
from natsort import natsorted
from tqdm.auto import tqdm

from collections.abc import Generator
from typing import ClassVar, Any
from typing_extensions import Self

from dapper_python.utils import yet_more_itertools
from dapper_python.databases_v2.database import Metadata
from dapper_python.databases_v2.python_db import PyPIDatabase, Package, PackageImport, PackageFile
from dapper_python.dataset_generation.utils.scraping import get_with_retry
from dapper_python.dataset_generation.utils.archive import SafeZipFile
from dapper_python.dataset_generation.utils.futures import BoundedThreadPoolExecutor

PYPI_INDEX_URL = 'https://pypi.python.org/simple/'


@dataclass
class PyPIPackage:
    package_name: str

    @methodtools.lru_cache(maxsize=1)
    def fetch_metadata(self) -> dict[str, Any]:
        """Gets the information contained on the package's PyPI page in json format

        :return: JSON-formatted data retrieved from the endpoint
        """
        url = self._API_PACKAGE_URL.format(package_name=self.package_name)
        with get_with_retry(url) as response:
            return response.json()

    def fetch_wheels(self) -> Generator[Wheel, None, None]:
        """Gets the wheel files for the package"""
        package_info = self.fetch_metadata()

        # Only keep ones that have wheels and have not been yanked
        releases = dict(natsorted(package_info['releases'].items(), reverse=True))
        releases = {
            version: data
            for version, data in releases.items()
            if any((
                x['packagetype'] == 'bdist_wheel'
                and not x['yanked']
                for x in data
            ))
        }
        if not releases:
            return None

        # Grab all wheels (for all architectures) from the latest version that has not been yanked and has some wheels
        version, release_data = next(iter(releases.items()))
        for entry in release_data:
            if not entry['packagetype'] == 'bdist_wheel':
                continue

            with get_with_retry(entry['url'], stream=True) as response:
                data = BytesIO(response.content)
            with suppress(zipfile.BadZipFile):
                yield Wheel(SafeZipFile(data))
        return None

    _API_PACKAGE_URL: ClassVar[str] = "https://pypi.org/pypi/{package_name}/json"


@dataclass
class Wheel:
    archive: ZipFile

    def __post_init__(self) -> None:
        self._exit_stack = ExitStack()

    def __enter__(self) -> Self:
        self._exit_stack.enter_context(self.archive)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return self._exit_stack.__exit__(exc_type, exc_val, exc_tb)

    def get_imports(self) -> set[str]:
        """Tries to get a list of names importable from the package"""
        return self._get_top_level_imports() | self._infer_imports()

    def _get_top_level_imports(self) -> set[str]:
        """Tries to get names importable from the package using the top-level.txt file"""
        package_files = [PurePosixPath(x) for x in self.archive.namelist()]

        # Sometimes contains a top_level.txt file which details the top-level imports for the package
        # If this is available, then use it as it's likely to be the most reliable information
        top_level_txt = next((x for x in package_files if x.name == "top_level.txt"), None)
        if not top_level_txt:
            return set()

        content = self.archive.read(str(top_level_txt)).decode("utf-8")
        imports = {line.strip() for line in content.splitlines() if line}
        return {x for x in imports if x}

    def _infer_imports(self) -> set[str]:
        """Tries to infer names importable from the package based on directory structure and contents

        Looks for .py files and directories containing __init__.py
        """
        package_files = [PurePosixPath(x) for x in self.archive.namelist()]

        top_level_paths = {
            entry.parents[-2] if len(entry.parents) >= 2 else entry
            for entry in package_files
        }

        # Check for any top-level python files, as these should also be importable
        importable_files = {
            file.stem
            for file in top_level_paths
            if file.suffix == ".py" and not file.name.startswith("_")
        }

        # Check for any top-level paths that contain an __init__.py
        importable_dirs = {
            directory.name
            for directory in top_level_paths
            if any((
                file.name == "__init__.py" and file.parent == directory
                for file in package_files
            ))
        }

        # TODO: Any other/better methods for determining importable names?
        # This seems to produce a fair amount of correct values, but also a fair number of duplicates across packages

        importable = importable_files | importable_dirs
        return {x for x in importable if x}

    def get_files(self) -> list[PackageFile]:
        """Gets a list of files in the archive along with their mime types and magic string

        The "magic string" is the output of running libmagic on the file, hence the name "magic" string
        Not that it is derived through unspecified means
        """
        files: list[PackageFile] = []
        for file in self.archive.namelist():
            # Needed to change comprehension to loop+add in order to support exception handling
            with suppress(zipfile.BadZipFile, zlib.error):
                raw_data = self.archive.read(file)

                try:
                    mime_type = magic.from_buffer(raw_data, mime=True)
                    magic_string = magic.from_buffer(raw_data)
                except magic.MagicException:
                    mime_type = None
                    magic_string = None
                files.append(PackageFile(
                    file_path=PurePosixPath(file),
                    mime_type=mime_type,
                    magic_string=magic_string,
                ))

        return files


def parse_package(name: str) -> Package | None:
    """Creates a Package object for the package of the specified name

    Downloads the package's wheel files, parses the imports and records the file contents
    Parses the result into a Package object which can be inserted into the database

    Needs to be a standalone function (callable) to be used with concurrent.futures
    """
    try:
        pypi_package = PyPIPackage(name)
        package_info = pypi_package.fetch_metadata()

        package = Package(
            package_name=name,
            last_serial=package_info["last_serial"],
        )

        wheel_files = more_itertools.peekable(pypi_package.fetch_wheels())
        if not wheel_files:
            return None

        imports: set[str] = set()
        files: dict[PurePosixPath, PackageFile] = {}  # Dict used for deduplication
        for wheel in wheel_files:
            with wheel:
                imports.update(wheel.get_imports())
                for pkg_file in wheel.get_files():
                    # Uses setdefault to only save the first occurrence
                    # If a file with the given path already exists, it won't be overwritten
                    files.setdefault(pkg_file.file_path, pkg_file)
        package.imports = [PackageImport(import_as=x) for x in imports]
        package.files = list(files.values())

        return package

    # If we can't access the data, skip for now and we'll try again later
    except (requests.exceptions.ConnectionError, requests.exceptions.RequestException):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Create Python imports DB from PyPI packages",
    )
    parser.add_argument(
        "-o", "--output",
        required=False,
        type=Path, default=Path("PyPIPackageDB.db"),
        help="Path of output (database) file to create. Defaults to \"PyPIPackageDB.db\" in the current working directory",
    )
    parser.add_argument(
        "-v", "--version",
        type=int, required=True,
        help="Version marker for the database to keep track of changes",
    )
    args = parser.parse_args()

    # Ask it to send the response as JSON
    # If we don't set the "Accept" header this way, it will respond with HTML instead of JSON
    json_headers = {
        "Accept": "application/vnd.pypi.simple.v1+json",
    }
    with requests.get(PYPI_INDEX_URL, headers=json_headers) as web_request:
        catalog_info = web_request.json()
        package_list = {
            entry["name"]: entry["_last-serial"]
            for entry in catalog_info["projects"]
        }

    pypi_db = PyPIDatabase.create_database(args.output, exist_ok=True)
    with pypi_db.session() as session:
        # Remove any outdated packages
        with session.begin():
            to_update = more_itertools.peekable((
                package
                for package in session.exec(select(Package))
                if package_list.get(package.package_name, package.last_serial) != package.last_serial
            ))
            progress_iter = tqdm(
                to_update,
                desc="Removing outdated packages",
                colour="red", unit="Package",
                disable=not to_update,
            )
            session.bulk_delete(progress_iter)

            # noinspection PyTypeChecker, Pydantic
            saved_packages: set[str] = set(session.exec(select(Package.package_name)))
            to_update = set(package_list.keys()) - saved_packages

        # Get new packages and add to the database
        TRANSACTION_SIZE = 250
        with BoundedThreadPoolExecutor() as pool:
            worker_tasks = (
                functools.partial(parse_package, name)
                for name in to_update
            )
            futures = pool.bounded_run(worker_tasks)

            progress_iter = tqdm(
                futures,
                total=len(to_update),
                desc="Scraping Packages", colour="blue",
                unit="Package",
                position=None, leave=None,
                disable=not to_update,
            )
            for chunk in yet_more_itertools.chunked_iter(progress_iter, TRANSACTION_SIZE):
                with session.begin():
                    packages = (pkg for future in chunk if (pkg := future.result()))
                    session.add_all(packages)

        # Reset the metadata if it already exists
        # Set version
        with session.begin():
            session.exec(delete(Metadata))
            session.add(Metadata(
                version=args.version,
                format="PyPI",
                timestamp=int(datetime.now().timestamp()),
            ))


if __name__ == "__main__":
    main()
