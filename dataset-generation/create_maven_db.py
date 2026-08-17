from __future__ import annotations

import argparse
import requests
import math
import time

from datetime import datetime
from pathlib import Path
from http import HTTPStatus
from tqdm.auto import tqdm

from dapper_python.databases_v2.database import Metadata
from dapper_python.databases_v2.maven_db import MavenDatabase, Package, PackageFile

MAVEN_API_URL = "https://search.maven.org/solrsearch/select"


def main():
    parser = argparse.ArgumentParser(
        description="Create java DB from Maven packages",
    )
    parser.add_argument(
        "-o", "--output",
        required=False,
        type=Path, default=Path("MavenPackageDB.db"),
        help="Path of output (database) file to create. Defaults to \"MavenPackageDB.db\" in the current working directory",
    )
    parser.add_argument(
        "-v", "--version",
        type=int, required=True,
        help="Version marker for the database to keep track of changes",
    )
    args = parser.parse_args()

    # Currently not set up to be able to handle resuming a previously started database
    # Due to the way the Maven API returns data, it needs to be done in one session
    if args.output.exists():
        raise FileExistsError(f"File {args.output} already exists")

    query_params = {
        "q": "*:*",  # Query all packages
        "rows": 0,  # Number of results per page
        "start": 0,  # Offset for pagination
        "wt": "json",  # JSON output
    }
    with requests.get(MAVEN_API_URL, params=query_params) as response:
        response.raise_for_status()
        init_data = response.json()
        num_entries = init_data["response"]["numFound"]
    if not num_entries:
        print("No packages found")
        return

    maven_db = MavenDatabase.create_database(args.output, exist_ok=False)
    with maven_db.session() as session:
        with session.begin():
            # Can request a maximum of 200 entries
            CHUNK_SIZE = 200

            progress_bar = tqdm(
                total=num_entries,
                desc="Processing packages", colour="green",
                unit="Package",
                position=None, leave=None,
                disable=not num_entries,
            )
            for page in range(math.ceil(num_entries / CHUNK_SIZE)):
                query_params = {
                    "q": "*:*",
                    "rows": CHUNK_SIZE,
                    "start": page,
                    "wt": "json",
                }
                with requests.get(MAVEN_API_URL, params=query_params) as response:
                    response.raise_for_status()

                    data = response.json()
                    pacakge_entries = data["response"]["docs"]

                    packages = []
                    for entry in pacakge_entries:
                        group_id, _, package_name = entry["id"].partition(":")
                        package = Package(
                            package_name=package_name,
                            group_id=group_id,
                            timestamp=entry["timestamp"],
                            files=[
                                PackageFile(file_name=entry["a"] + suffix)
                                for suffix in entry["ec"]
                            ],
                        )
                        packages.append(package)
                    session.bulk_insert(packages)
                    progress_bar.update(len(pacakge_entries))

                    # Try to rate-limit the requests since it's causing problems
                    time.sleep(1)

        # Set version
        with session.begin():
            session.add(Metadata(
                version=args.version,
                format="Maven",
                timestamp=int(datetime.now().timestamp()),
            ))


if __name__ == "__main__":
    main()
