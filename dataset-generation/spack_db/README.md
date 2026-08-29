# Spack Build Cache Data Scraper & SQLite Database

This project aims to scrape the Spack build cache by downloading, cleaning, and indexing spec manifests and binary tarballs into a local cache, then convert the data into a Spack SQLite database. 

The program builds a master index called `spack.index.db.json`. 
* Index layout:
    * spec manifest hash as the unique key
    * package name and version
    * package tarball unique SHA256 hash
    * package manifest path to the local cache directory
    * package tarinfo path to the local cache directory

The program allows for restart/resume in case there are program run interruptions. Skipped or malformed manifests are recorded and if the information exists for both manifest and tarball, re-downloading files is avoided. 

## Directory Structure
* `cache/spack.index.db.json` - master index
* `cache/manifest/` - cleaned spec manifests
* `cache/tarinfo/` - JSON file lists extracted from tarballs
* `cache/binary_packages/` - temporary cache of downloaded tarballs
* `cache/timeouts.txt` - packages that timed out while downloading
* `cache/skipped_manifests.txt` - a list of manifests that could not be downloaded
* `cache/malformed_manifests.txt` - manifests that failed parsing
* `cache/missing_tarballs.txt` - manifests without a tarball hash
* `cache/shared_tarballs.txt` - records multiple manifests that point to the same tarball
* `cache/failed_tarball_downloads.txt` - tarballs that failed to download

## Features
* Retrieves package `.spec.manifest.json` from Spack's binary mirror
* Extracts valid JSON payload, and removes extra characters
* Retrieves binary tarballs and extracts file lists
* Creates and maintains a canonical JSON index that maps package to it's manifest and tarball information
* Contains multiple checkpoints for safe restart/resume of the program
* Records skipped/malformed manifests, missing hashes, failed tarbll downloads
* Stores forward-slash paths in JSON index for cross-platform use

## Usage
1. Install dependencies
    ```bash
    pip install requests
    ```
    The rest of the necessary modules are part of Python's standard library.

2. Provide a database file
    Update the file_name in `main()` if needed

3. Run the script
    ```bash
    python spack_db.py
    ```

4. Resume after interruption
    If an interruption occurs, it is safe to re-run the script without losing data already processed. 

5. Retry manifests or tarballs
    Delete the files `skipped_manifests.txt`, `malformed_manifests.txt`, `failed_tarball_downloads.txt`, to retry failed manifest or tarball downloads.

6. Run Create_spack_DB.py to create SQLite database
    ```bash
    python Create_spack_DB.py
    ```
    Database will include all files extracted from the packages from the Spack build cache.
