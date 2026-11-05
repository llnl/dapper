# /// script
# dependencies = [
#   "requests",
#   "python-debian",
#   "tqdm",
#   "typing-extensions",
#
#   "dapper-python",
# ]
# ///

"""
This script processes the "Linux Contents" file and parses which files are added by which packages
An example of this file can be found here: http://security.ubuntu.com/ubuntu/dists/focal/Contents-amd64.gz

The result is stored in a sqlite database

The database has one table:
    "package_files"
The table has five main columns:
    file_name               - Just the name of the file
                              ex: "lib/modules/5.4.0-1009-aws/vdso/vdso32.so" -> vdso32.so
    normalized_file_name    - The filename normalized to remove version info
                              ex: "libexample-1.2.3.so" -> "libexample.so"
    file_path               - The entire path for the file
                              ex: "lib/modules/5.4.0-1009-aws/vdso/vdso32.so"
    package_name            - The short package name
                              ex: "admin/multipath-tools" -> multipath-tools
    full_package_name       - The full/long package name
                              ex: admin/multipath-tools

The file_name column is indexed for fast lookups, as the file_name is the primary value that will be searched for
"""
from __future__ import annotations

import argparse
import requests
import sqlite3
import gzip
import lzma

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from datetime import datetime
from io import BytesIO, FileIO, TextIOWrapper
from urllib.parse import urlparse
from tqdm.auto import tqdm
from debian.deb822 import Deb822

from typing_extensions import Self

from dapper_python.databases.database import Database
from dapper_python.normalize import NormalizedFileName, normalize_file_name


@dataclass
class PackageDetails:
    full_package_name: str
    file_path: PurePosixPath

    @property
    def package_name(self) -> str:
        return self.full_package_name.rsplit('/', maxsplit=1)[-1]

    @property
    def file_name(self) -> str:
        return self.file_path.name

    def __post_init__(self):
        if not isinstance(self.file_path, PurePosixPath):
            self.file_path = PurePosixPath(self.file_path)

    @classmethod
    def from_linux_package_file(cls, line:str) -> Self:
        """Creates a PackageDetails object out of a single line from the linux contents file
        Uses simple parsing to split the line into package_name and file_path and then construct the PackageDetails object

        :param line: A line of text from the linux contents file
        :return: The package info for that line
        """
        file_path, full_package_name = tuple(x.strip() for x in line.rsplit(maxsplit=1))
        return cls(
            full_package_name=full_package_name,
            file_path=PurePosixPath(file_path)
        )

@dataclass
class SourceDetails:
    package: str
    bin_packages: list[str]

    #There could be further information extracted from the Sources file
    #But this is all we currently need

    @classmethod
    def from_sources_file(cls, entry:Deb822) -> Self:
        return cls(
            package=entry.get('Package'),
            bin_packages=[
                x.strip()
                for x in entry.get('Binary').split(',')
            ],
        )



class LinuxDatabase(Database):

    def __init__(self, db_path:Path) -> None:
        super().__init__(db_path, mode='rwc')
        self._init_database()

    def _init_database(self) -> None:
        with self.cursor() as cursor:
            #Would there be any benefit to having a separate package table
            #Which the files table references as a foreign key vs directly saving the package into the files table?
            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS package_files(
                    id INTEGER PRIMARY KEY,
                    file_name TEXT,
                    normalized_file_name TEXT,
                    file_path TEXT,
                    package_name TEXT,
                    full_package_name TEXT
                )
            """
            cursor.execute(create_table_cmd)

            #Index the filename column for fast lookups
            #Currently does not index package name as use case does not require fast lookups on package name and reduces filesize
            index_cmd = """
                CREATE INDEX idx_file_name
                ON package_files(file_name);
            """
            cursor.execute(index_cmd)
            index_cmd = """
                CREATE INDEX idx_normalized_file_name
                ON package_files(normalized_file_name);
            """
            cursor.execute(index_cmd)


            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS package_sources(
                    id INTEGER PRIMARY KEY,
                    package_name TEXT,
                    bin_package TEXT
                )
            """
            cursor.execute(create_table_cmd)

            #Index the binary package column (packages built from this one) for fast lookups
            index_cmd = """
                CREATE INDEX idx_bin_packages
                ON package_sources(bin_package);
            """
            cursor.execute(index_cmd)


            #Create combined view for easier querying
            create_view_cmd = """
                CREATE VIEW
                IF NOT EXISTS v_package_files
                AS
                    SELECT file_name, normalized_file_name, file_path, package_files.package_name AS package_name, full_package_name, package_sources.package_name AS source_package_name
                    FROM package_files
                    LEFT OUTER JOIN package_sources
                    ON package_files.package_name = package_sources.bin_package
            """
            cursor.execute(create_view_cmd)


            #Metadata information about dataset
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
            metdata_remove_cmd = """
                DELETE FROM dataset_version
            """
            cursor.execute(metdata_remove_cmd)

            metadata_add_cmd = """
                INSERT INTO dataset_version(version, format, timestamp)
                VALUES (?, "Linux", ?)
            """
            cursor.execute(metadata_add_cmd, (version, int(datetime.now().timestamp())))

    def add_package(self, package_details:PackageDetails) -> None:
        #Lower seems like it should work? As far as the OS is concerned ß.json is not the same file as ss.json
        normalized_file = normalize_file_name(package_details.file_name)
        match normalized_file:
            case str(name):
                normalized_file_name = name.lower()
            case NormalizedFileName():
                normalized_file_name = normalized_file.name.lower()
            case _:
                raise TypeError(f"Failed to normalize file: {package_details.file_name}")

        cursor = self.cursor()
        insert_cmd = """
            INSERT INTO package_files(file_name, normalized_file_name, file_path, package_name, full_package_name)
            VALUES (?, ?, ?, ?, ?)
        """

        cursor.execute(
            insert_cmd,
            (package_details.file_name, normalized_file_name, str(package_details.file_path),
             package_details.package_name, package_details.full_package_name,)
        )

    def add_source(self, source_details:SourceDetails) -> None:
        cursor = self.cursor()
        insert_cmd = """
            INSERT INTO package_sources(package_name, bin_package)
            VALUES (?, ?)
        """
        data = (
            (source_details.package, bin_package)
            for bin_package in source_details.bin_packages
        )
        cursor.executemany(insert_cmd, data)


def read_data(uri: str | Path, *, encoding='utf-8') -> TextIOWrapper:
    """Reads a file either from disk or by downloading it from the provided URL
    Will attempt to read the provided file as a text file

    :param uri: Filepath on disk, or URL to download from
    :param encoding: The text encoding to of the file, normally utf-8
    :return: A TextIOWrapper around the file. Can iterate over lines
    """
    if isinstance(uri, Path):
        if not uri.exists():
            raise FileNotFoundError(f"File {uri} does not exist")
        return TextIOWrapper(FileIO(uri, mode='rb'), encoding=encoding)

    elif isinstance(uri, str):
        parsed_url = urlparse(uri)
        if not (parsed_url.scheme and parsed_url.netloc):
            raise ValueError(f"Invalid URL: {uri}")

        with requests.get(uri, stream=True) as web_request:
            if 'content-length' in web_request.headers:
                file_size = int(web_request.headers['content-length'])
            else:
                file_size = None

            content = BytesIO()
            progress_bar = tqdm(
                total=file_size,
                desc='Downloading file', colour='blue',
                unit='B', unit_divisor=1024, unit_scale=True,
                position=None, leave=None,
            )
            with progress_bar:
                for chunk in web_request.iter_content(chunk_size=8*1024):
                    content.write(chunk)
                    progress_bar.update(len(chunk))
            content.seek(0)

            #Data is most commonly in a compressed gzip format, but support some others as well
            match web_request.headers.get('Content-Type', None):
                case 'application/x-gzip':
                    with gzip.open(content) as gz_file:
                        return TextIOWrapper(BytesIO(gz_file.read()), encoding=encoding)
                case 'application/x-xz':
                    with lzma.open(content) as lzma_file:
                        return TextIOWrapper(BytesIO(lzma_file.read()), encoding=encoding)
                case _:
                    #Not sure, try to read as raw text file
                    return TextIOWrapper(content)

    else:
        raise TypeError(f"Invalid input: {uri}")

def main():
    parser = argparse.ArgumentParser(
        description="Create Linux DB by parsing the Linux Contents file"
    )
    #Allow to be either a path or a URL
    parser.add_argument(
        '-c','--contents',
        required=True,
        type=lambda x: str(x) if urlparse(x).scheme and urlparse(x).netloc else Path(x),
        help='Path or URL to linux contents file',
    )
    parser.add_argument(
        '-s','--sources',
        required=True,
        type=lambda x: str(x) if urlparse(x).scheme and urlparse(x).netloc else Path(x),
        help='Path or URL to linux sources file',
    )
    parser.add_argument(
        '-o','--output',
        required=False,
        type=Path, default=Path('LinuxPackageDB.db'),
        help='Path of output (database) file to create. Defaults to "LinuxPackageDB.db" in the current working directory',
    )
    parser.add_argument(
        '-v', '--version',
        type=int, required=True,
        help='Version marker for the database to keep track of changes'
    )
    args = parser.parse_args()

    #Currently not set up to be able to handle resuming a previously started database
    #However it's not a high priority as the process only takes a minute or two. Can just delete the old DB and recreate
    #TODO: Potentially allow resuming in the future
    if args.output.exists():
        raise FileExistsError(f"File {args.output} already exists")

    with LinuxDatabase(args.output) as db:
        #Process entries in linux contents file
        file = read_data(args.contents)
        entry_count = sum(1 for _ in file)
        file.seek(0)

        progress_iter = tqdm(
            file,
            total=entry_count,
            desc='Processing Contents', colour='green',
            unit='Entry',
        )
        for entry in progress_iter:
            package = PackageDetails.from_linux_package_file(entry)
            db.add_package(package)

        #Process entries in linux sources file
        file = read_data(args.sources)
        entry_count = sum(1 for _ in Deb822.iter_paragraphs(file))
        file.seek(0)

        progress_iter = tqdm(
            Deb822.iter_paragraphs(file),
            total=entry_count,
            desc='Processing Sources', colour='cyan',
            unit='Entry',
        )
        for entry in progress_iter:
            package = SourceDetails.from_sources_file(entry)
            db.add_source(package)

        db.set_version(args.version)

if __name__ == "__main__":
    main()