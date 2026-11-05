# /// script
# dependencies = [
#   "requests",
#   "tqdm",
#   "more-itertools",
#   "methodtools",
#   "natsort",
#   "typing-extensions",
#
#   "python-magic-bin",
#
#   "dapper-python",
# ]
# ///

"""
This script processes all packages listed on PyPI's index to creates a database of what package(s) correspond to each import name
The index of all listed packages can be found here: https://pypi.python.org/simple/

The result is stored in a sqlite database

The database has two tables:
    "packages" and "package_imports"
However the best method is to use the view "v_package_imports" which omits some of the columns used for tracking
Which are unnecessary for end-users
The table has two columns:
    package_name        - The name of the package                   ex: "beautifulsoup4"
    import_as           - The name used to import the package       ex: "bs4"

The import_as column is indexed for fast lookups, as the import_as is the primary value that will be searched for

Since the scraping process can take several hours to complete, this is designed to be able to stop and restart mid-process
To avoid duplicating time-consuming work should it be stopped part-way through
"""
from __future__ import annotations

import argparse
import requests
import sqlite3
import zipfile, zlib
import math
import time
import io
import more_itertools
import concurrent.futures
import functools
import methodtools

#On Windows/Mac: Install "python-magic-bin" from pip to also get executable, don't install python-magic, python-libmagic, etc
#On Linux, this doesn't work because it doesn't have linux executables
#But can potentially just install python-magic which will use the underlying linux utilities
import magic

from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from http import HTTPStatus
from contextlib import suppress
from natsort import natsorted
from zipfile import ZipFile
from tqdm.auto import tqdm
from itertools import repeat
from more_itertools import chunked

from typing import Final, ClassVar, Literal
from typing import TypeVar, Callable, ParamSpec
from typing import Generator, Iterable, Any
from typing_extensions import Self

from dapper_python.databases.database import Database
from dapper_python.normalize import normalize_file_name


@dataclass
class PackageDetails:
    """Container class for storing information about a package that we want to save to the database

    Acts as a convent single-object to return from processing function to make calling via concurrent.futures cleaner
    """
    name: str
    serial: int|None

    version: str|None = None
    imports: set[str] = field(default_factory=set)
    files: set[FileDetails] = field(default_factory=set)

@dataclass(frozen=True)
class FileDetails:
    """Details about an individual file

    Includes extra information about the type of the file which may not be obvious just from its extension
    mime_type is the result of asking libmagic for the mime type
    and magic_string is the full-length description that libmagic returns
    """
    file: PurePosixPath
    mime_type: str|None = field(default=None, compare=False, hash=False)
    magic_string: str|None = field(default=None, compare=False, hash=False)



class PyPIDatabase(Database):
    """Handles reading from and writing to the database

    SQLite doesn't support stored procedures, so this class contains several pre-defined functions (e.g add_package_imports)
    Which take their place of an API-of-sorts for an operation with several backend steps
    """

    def __init__(self, db_path:Path):
        super().__init__(db_path, mode='rwc')
        self._init_database()

    def _init_database(self) -> None:
        """Initializes the database to create the required tables, indexes, and views
        """
        with self.cursor() as cursor:
            #The last_serial column is used for checking values when resuming from a partially-built DB or updating the DB
            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS packages(
                    id INTEGER PRIMARY KEY,
                    package_name TEXT,
                    last_serial INTEGER
                )
            """
            cursor.execute(create_table_cmd)
            # create_index_cmd = """
            #     CREATE INDEX
            #     IF NOT EXISTS idx_package_name
            #     ON packages(package_name);
            # """
            # cursor.execute(create_index_cmd)

            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS package_imports(
                    id INTEGER PRIMARY KEY,
                    package_id INTEGER,
                    import_as TEXT,
                    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
                )
            """
            cursor.execute(create_table_cmd)
            create_index_cmd = """
                CREATE INDEX
                IF NOT EXISTS idx_package_id
                ON package_imports(package_id);
            """
            cursor.execute(create_index_cmd)
            create_index_cmd = """
                CREATE INDEX
                IF NOT EXISTS idx_import_as
                ON package_imports(import_as);
            """
            cursor.execute(create_index_cmd)

            #User-facing view for imports which hides the backend tracking logic
            create_view_cmd = """
                CREATE VIEW
                IF NOT EXISTS v_package_imports
                AS 
                    SELECT package_name, import_as
                    FROM packages
                    JOIN package_imports
                    ON packages.id = package_imports.package_id
            """
            cursor.execute(create_view_cmd)

            create_table_cmd = """
            CREATE TABLE
            IF NOT EXISTS package_files(
                id INTEGER PRIMARY KEY,
                package_id INTEGER,
                file_name TEXT,
                normalized_file_name TEXT,
                file_path TEXT,
                mime_type TEXT,
                magic_string TEXT,
                FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
            )
            """
            cursor.execute(create_table_cmd)

            #User-facing view for files which hides the backend tracking logic
            create_view_cmd = """
                CREATE VIEW
                IF NOT EXISTS v_package_files
                AS 
                    SELECT package_name, normalized_file_name, file_name, file_path, mime_type, magic_string
                    FROM packages
                    JOIN package_files
                    ON packages.id = package_files.package_id
            """
            cursor.execute(create_view_cmd)

            #Metadata information about table
            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS dataset_version(
                    version INTEGER PRIMARY KEY,
                    format TEXT,
                    timestamp INTEGER
                )
            """
            cursor.execute(create_table_cmd)


    def set_version(self, version:int) -> None:
        with self.cursor() as cursor:
            #We only want a single version information row, so if the table already has values, clear it
            metadata_remove_cmd = """
                DELETE FROM dataset_version
            """
            cursor.execute(metadata_remove_cmd)

            metadata_add_cmd = """
                INSERT INTO dataset_version(version, format, timestamp)
                VALUES (?, "PyPI", ?)
            """
            cursor.execute(metadata_add_cmd, (version, int(datetime.now().timestamp())))

    def get_processed_packages(self) -> Generator[tuple[str,int], None, None]:
        """Gets a list of all the packages that have been added to the database along with their serial number

        This is mainly used for comparing what values are already in the database against any remaining values to scrape
        (In case the process was stopped mid-way)

        :return: A generator of tuples with the format (package_name, serial)
        """
        cursor = self.cursor()
        package_query = """
            SELECT package_name, last_serial
            FROM packages
        """
        packages = (
            (package_name, last_serial)
            for package_name, last_serial, *_ in cursor.execute(package_query).fetchall_chunked()
        )
        yield from packages

    def add_package(self, package_details:PackageDetails) -> None:
        """Adds a package and import names to the database

        :param package_name: Name of the package
        :param package_imports: Any top-level names that are imported from the package
        :param serial: A number designed to keep track of when the package was processed
                       Intended to be the last_serial field from the PyPI index, but could be a custom value
        """
        if not package_details.imports and not package_details.files:
            #Nothing useful to add to the database
            return

        with self.cursor() as cursor:
            insert_package_cmd = """
                INSERT INTO packages(package_name, last_serial)
                values (?, ?)
            """
            cursor.execute(insert_package_cmd, (package_details.name, package_details.serial))

            #We need the saved id in order to reference as foreign key in the package_imports table
            package_id = cursor.lastrowid

            if package_details.imports:
                insert_import_cmd = """
                    INSERT INTO package_imports(package_id, import_as)
                    values (?, ?)
                """
                cursor.executemany(insert_import_cmd, zip(repeat(package_id), package_details.imports))

            if package_details.files:
                insert_file_cmd = """
                    INSERT INTO package_files(package_id, file_name, normalized_file_name, file_path, mime_type, magic_string)
                    values (?, ?, ?, ?, ?, ?)
                """
                data = (
                    (package_id, file.file.name, str(normalize_file_name(file.file.name)), str(file.file), file.mime_type, file.magic_string)
                    for file in package_details.files
                )
                cursor.executemany(insert_file_cmd, data)

    def remove_packages(self, package_names:str|Iterable[str]) -> None:
        """Removes the specified package(s) from the database

        This will remove the package from the "packages" table
        Along with associated entries in "package_imports" and "package_files" tables

        :param package_names: Name(s) of the package(s) to remove
        """
        if isinstance(package_names, str):
            package_names = (package_names, )

        with self.cursor() as cursor:
            remove_package_cmd = """
            DELETE FROM packages
            WHERE package_name = ?
            """
            cursor.executemany(remove_package_cmd, ((name,) for name in package_names))

    PYPI_INDEX_URL:ClassVar[str] = 'https://pypi.python.org/simple/'


@dataclass
class PyPIPackage:
    package_name: str

    @methodtools.lru_cache(maxsize=1)
    def get_package_info(self) -> dict[str, Any]:
        """Gets the information contained on the package's PyPI page in json format

        :return: JSON-formatted data retrieved from the endpoint
        """
        url = self._API_PACKAGE_URL.format(package_name=self.package_name)
        return self._web_request(url).json()

    def _get_wheel_files(self) -> Generator[ZipFile, None, None]:
        package_info = self.get_package_info()

        #Only keep ones that have wheels and have not been yanked
        releases = dict(natsorted(package_info['releases'].items(), reverse=True))
        releases = {
            version:data
            for version, data in releases.items()
            if any(
                x['packagetype'] == 'bdist_wheel'
                and not x['yanked']
                for x in data
            )
        }
        if not releases:
            return None

        #Grab all wheels (for all architectures) from the latest version that has not been yanked and has some wheels
        version, release_data = next(iter(releases.items()))
        for entry in release_data:
            if not entry['packagetype'] == 'bdist_wheel':
                continue

            with self._web_request(entry['url'], stream=True) as web_request:
                data = io.BytesIO(web_request.content)
            with suppress(zipfile.BadZipFile):
                yield ZipFile(data)

    def _get_imports(self, wheel_file:ZipFile) -> set[str]:
        imports = set()
        package_files = list(PurePosixPath(x) for x in wheel_file.namelist())

        #Sometimes contains a top_level.txt file which details the top-level imports for the package
        #If this is available, then use it as it's likely to be the most reliable information
        top_level_txt = next((x for x in package_files if x.name == 'top_level.txt'), None)
        if top_level_txt:
            text_data = wheel_file.read(str(top_level_txt)).decode('utf-8')
            imports = set(line.strip() for line in text_data.splitlines() if line)
            imports.update(imports)

            #Sometimes these files can be empty
            if imports:
                #TODO: If a top_level.txt is present, does this preclude other imports?
                #I.e. Should we keep checking or stop here?
                return imports

        #TODO: Any other/better methods for determining importable names?
        #This seems to produce a fair amount of correct values, but also a fair number of duplicates across packages

        #If it doesn't have that file or couldn't parse it
        #Then fall back on trying to check what directories are importable as modules
        top_level_paths = tuple({
            PurePosixPath(x.parents[-2] if len(x.parents) >= 2 else x)
            for x in package_files
        })

        #Check for any top-level python files, as these should also be importable
        importable_files = {
            file.stem
            for file in top_level_paths
            if file.name.endswith('.py')
            and not file.name.startswith('_')
        }

        #Check for any top-level paths that contain an __init__.py
        importable_dirs = {
            directory.name
            for directory in (Path(y) for y in top_level_paths)
            if any(
                file.name == '__init__.py'
                and file.parent == directory
                for file in package_files
            )
        }

        importable = set(x for x in importable_files | importable_dirs if x)
        imports.update(importable)
        return imports


    def _get_file_list(self, wheel_file:ZipFile) -> set[FileDetails]:
        files = set()
        for file in wheel_file.namelist():
            #Needed to change comprehension to loop+add in order to support exception handling
            with suppress(zipfile.BadZipFile, zlib.error):
                raw_data = wheel_file.read(file)
                files.add(FileDetails(
                    file=PurePosixPath(file),
                    mime_type=magic.from_buffer(raw_data, mime=True),
                    magic_string=magic.from_buffer(raw_data),
                ))
        return files

    def get_package_details(self) -> PackageDetails|None:
        """Scrapes relevant details about the package to save in the database

        Downloads the .whl (wheel) file for the package and uses contained information to determine how the package is imported
        This may differ from the name of the package itself
        eg: BeautifulSoup4 is installed as "pip install beautifulsoup4" but used in source code as "import bs4"

        Additionally, records all the files that the package contains

        :return: A list of names that can be imported from the package
                 If there are no usable releases of the package, None is returned instead
        """
        wheel_files = more_itertools.peekable(self._get_wheel_files())
        try:
            wheel_files.peek()
        except StopIteration:
            return None

        package_info = self.get_package_info()
        package_details = PackageDetails(name=self.package_name, serial=package_info['last_serial'])
        for wheel_file in wheel_files:
            with wheel_file as wheel_data:
                package_details.imports.update(self._get_imports(wheel_data))
                package_details.files.update(self._get_file_list(wheel_data))
        return package_details

    @staticmethod
    def _web_request(url:str, *, retries:int=5, **kwargs) -> requests.Response:
        """Attempts to retrieve the web content from the specified URL

        From a software design perspective, this doesn't necessarily belong in this class
        But this is the only class accesses the internet, so it's a convent spot to put it

        :param url: The URL to retrieve the content from
        :param retries: Number of times to retry the request if it fails to respond with an HTTP OK status
        :return: A requests response from sending a GET request to the specified URL
        """
        for _ in range(retries+1):
            try:
                web_request = requests.get(url, **kwargs)

                match web_request.status_code:
                    case HTTPStatus.OK:
                        return web_request
                    case HTTPStatus.TOO_MANY_REQUESTS:
                        delay = web_request.headers.get('Retry-After', 1)
                        time.sleep(delay)
                        continue
                    case HTTPStatus.NOT_FOUND:
                        break
                    case _:
                        #TODO: How do we want to handle other status codes? For now retry
                        continue

            except requests.exceptions.ConnectionError:
                time.sleep(1)
                continue

        raise requests.exceptions.RequestException('Could not get requested data')

    #============================== Class Attributes ==============================#
    _API_PACKAGE_URL:ClassVar[str] = 'https://pypi.org/pypi/{package_name}/json'


def main():
    parser = argparse.ArgumentParser(
        description="Create Python imports DB from PyPI packages"
    )
    parser.add_argument(
        '-o','--output',
        required=False,
        type=Path, default=Path('PyPIPackageDB.db'),
        help='Path of output (database) file to create. Defaults to "PyPIPackageDB.db" in the current working directory',
    )
    parser.add_argument(
        '-v', '--version',
        type=int, required=True,
        help='Version marker for the database to keep track of changes'
    )
    args = parser.parse_args()

    #Ask it to send the response as JSON
    #If we don't set the "Accept" header this way, it will respond with HTML instead of JSON
    json_headers = {
        'Accept': 'application/vnd.pypi.simple.v1+json'
    }
    with requests.get(PyPIDatabase.PYPI_INDEX_URL, headers=json_headers) as web_request:
        catalog_info = web_request.json()
        package_list = {
            entry['name']:entry['_last-serial']
            for entry in catalog_info['projects']
        }

    with PyPIDatabase(args.output) as db:
        #Remove any outdated packages
        processed_packages = dict(db.get_processed_packages())
        to_remove = [
            name
            for name, serial in package_list.items()
            if name in processed_packages
            and processed_packages[name] != serial
        ]
        db.remove_packages(to_remove)

        #Only process packages which have not already been processed
        processed_packages = dict(db.get_processed_packages())
        to_process = {
            name:serial
            for name,serial in package_list.items()
            if name not in processed_packages
        }

        #Break into chunks to process
        CHUNK_SIZE:Final[int] = 500
        chunked_entries = chunked(to_process.keys(), CHUNK_SIZE) #Process 500 at a time to speed up
        chunk_count = int(math.ceil(len(to_process) / CHUNK_SIZE))

        progress_iter = tqdm(
            chunked_entries,
            total=chunk_count,
            desc='Processing package slice', colour='green',
            unit='Slice',
            position=None, leave=None,
            disable=False,
        )
        for chunk in progress_iter:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                threads = [
                    pool.submit(PyPIPackage(name).get_package_details)
                    for name in chunk
                ]

                progress_bar = tqdm(
                    concurrent.futures.as_completed(threads),
                    total=len(threads),
                    desc='Scraping Package', colour='blue',
                    unit='Package',
                    position=None, leave=None,
                    disable=not to_process,
                )

                for future in progress_bar:
                    with suppress(requests.exceptions.ConnectionError, requests.exceptions.RequestException):
                        package_details = future.result()
                        if not package_details:
                            continue
                        db.add_package(package_details)

        db.set_version(args.version)

if __name__ == '__main__':
    main()