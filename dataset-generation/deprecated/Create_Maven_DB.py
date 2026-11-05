# /// script
# dependencies = [
#   "requests",
#   "tqdm",
#   "typing-extensions",
#
#   "dapper-python",
# ]
# ///
from __future__ import annotations

import argparse

import requests
import math
import time

from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from http import HTTPStatus
from tqdm.auto import tqdm

from typing import Final, ClassVar, Literal
from typing import Generator, Iterable, Any
from typing_extensions import Self

from dapper_python.databases.database import Database


@dataclass
class PackageDetails:
    package_name: str
    group_id: str
    pacakge_files: list[str]

    repo_id: str|None = None
    timestamp: int|None = None

    @classmethod
    def from_maven_entry(cls, data:dict[str, Any]) -> Self:
        group_id, _, package_name = data['id'].partition(':')
        package_files = [
            data['a'] + suffix
            for suffix in data['ec']
        ]

        return cls(
            package_name=package_name,
            group_id=group_id,
            pacakge_files=package_files,
            repo_id=data['repositoryId'],
            timestamp=data['timestamp'],
        )


class MavenDatabase(Database):

    def __init__(self, db_path:Path):
        super().__init__(db_path, mode='rwc')
        self._init_database()

    def _init_database(self) -> None:
        with self.cursor() as cursor:

            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS packages(
                    id INTEGER PRIMARY KEY,
                    group_id TEXT,
                    package_name TEXT,
                    timestamp INTEGER
                )
            """
            cursor.execute(create_table_cmd)

            create_index_cmd = """
                CREATE INDEX
                IF NOT EXISTS idx_packages
                ON packages(package_name);
            """
            cursor.execute(create_index_cmd)

            create_table_cmd = """
                CREATE TABLE
                IF NOT EXISTS package_files(
                    id INTEGER PRIMARY KEY,
                    package_id INTEGER,
                    file_name TEXT,
                    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
                )
            """
            cursor.execute(create_table_cmd)

            create_index_cmd = """
                CREATE INDEX
                IF NOT EXISTS idx_package_file
                ON package_files(file_name);
            """
            cursor.execute(create_index_cmd)

            #User-facing view for files which hides the backend tracking logic
            create_view_cmd = """
                CREATE VIEW
                IF NOT EXISTS v_package_files
                AS 
                    SELECT package_name, file_name
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
                VALUES (?, "Maven", ?)
            """
            cursor.execute(metadata_add_cmd, (version, int(datetime.now().timestamp())))

    def add_package(self, package_details:PackageDetails) -> None:
        cursor = self.cursor()

        insert_package_cmd = """
            INSERT INTO packages(group_id, package_name, timestamp)
            VALUES (?, ?, ?)
        """
        cursor.execute(
            insert_package_cmd,
            (package_details.group_id, package_details.package_name, package_details.timestamp)
        )

        #We need the saved id in order to reference as foreign key in the package_imports table
        package_id = cursor.lastrowid

        insert_package_files_cmd = """
            INSERT INTO package_files(package_id, file_name)
            VALUES (?, ?)
        """
        data = (
            (package_id, file_name)
            for file_name in package_details.pacakge_files
        )
        cursor.executemany(insert_package_files_cmd, data)

    MAVEN_API_URL:ClassVar[str] = 'https://search.maven.org/solrsearch/select'


def main():
    parser = argparse.ArgumentParser(
        description="Create java DB from Maven packages"
    )
    parser.add_argument(
        '-o','--output',
        required=False,
        type=Path, default=Path('MavenPackageDB.db'),
        help='Path of output (database) file to create. Defaults to "MavenPackageDB.db" in the current working directory',
    )
    parser.add_argument(
        '-v', '--version',
        type=int, required=True,
        help='Version marker for the database to keep track of changes'
    )
    args = parser.parse_args()

    init_params = {
        "q": "*:*",         # Query all packages
        "rows": 0,          # Number of results per page
        "start": 0,         # Offset for pagination
        "wt": "json",       # JSON output
    }

    with requests.get(MavenDatabase.MAVEN_API_URL, params=init_params) as response:
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(f'Could not access api: {response.status_code}\n{response.content}')
        init_data = response.json()
        num_entries = init_data['response']['numFound']

    #Can request a maximum of 200 entries
    CHUNK_SIZE:Final[int] = 200

    with MavenDatabase(args.output) as db:
        try:
            progress_bar = tqdm(
                total=num_entries,
                desc='Processing packages', colour='green',
                unit='Package',
                position=None, leave=None,
                disable=not num_entries,
            )
            for page in range(math.ceil(num_entries / CHUNK_SIZE)):
                params = {
                    "q": "*:*",
                    "rows": CHUNK_SIZE,
                    "start": page,
                    "wt": "json",
                }
                with requests.get(MavenDatabase.MAVEN_API_URL, params=params) as response:
                    if response.status_code != HTTPStatus.OK:
                        raise RuntimeError(f'Could not access api: {response.status_code}\n{response.content}')

                    data = response.json()
                    pacakge_entries = data['response']['docs']
                    for entry in pacakge_entries:
                        package_details = PackageDetails.from_maven_entry(entry)
                        db.add_package(package_details)
                    progress_bar.update(len(pacakge_entries))

                #Try to rate-limit the requests since it's causing problems
                time.sleep(1)

            #Concurrent implementation that tries to read multiple pages at a time for faster scraping
            #Currently not used due to rate limiting
            # with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            #     threads = [
            #         pool.submit(requests.get, MavenDatabase.MAVEN_API_URL, params={
            #             "q": "*:*",
            #             "rows": CHUNK_SIZE,
            #             "start": page,
            #             "wt": "json",
            #         })
            #         for page in range(math.ceil(num_entries / CHUNK_SIZE))
            #     ]
            #
            #     progress_bar = tqdm(
            #         concurrent.futures.as_completed(threads),
            #         total=num_entries,
            #         desc='Processing packages', colour='green',
            #         unit='Package',
            #         position=None, leave=None,
            #         disable=not num_entries,
            #     )
            #
            #     for future in progress_bar:
            #         with future.result() as response:
            #             if response.status_code != HTTPStatus.OK:
            #                 raise RuntimeError(f'Could not access api: {response.status_code}\n{response.content}')
            #
            #             data = response.json()
            #             pacakge_entries = data['response']['docs']
            #             for entry in pacakge_entries:
            #                 package_details = PackageDetails.from_maven_entry(entry)
            #                 db.add_package(package_details)
            #         progress_bar.update(len(pacakge_entries))

        finally:
            #We still want to set the version and commit, even if an error happens
            db.set_version(args.version)

if __name__ == '__main__':
    main()