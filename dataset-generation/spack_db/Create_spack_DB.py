import os
import json
import sqlite3
import time
from dapper_python.normalize import normalize_file_name

# configuration
INDEX_PATH = "cache/spack.index.db.json"
SQLITE_DB_PATH = "cache/spack-v1.db"

def build_package_filelist_db():
    # load index
    if not os.path.exists(INDEX_PATH):
        print("❌ Index file not found.")
        return

    with open(INDEX_PATH, "r") as f:
        index = json.load(f)

    # Create SQLite DB
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()

    # Create table columns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS package_files (
            id INTEGER PRIMARY KEY,
            file_name TEXT,
            normalized_file_name TEXT,
            file_path TEXT,
            package_name TEXT,
            UNIQUE(file_path, package_name)
        )
    ''')

    # Create indices for efficient lookups
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_name ON package_files(file_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_normalized_file_name ON package_files(normalized_file_name)')


    # Create dataset_version table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dataset_version(
                   version INTEGER,
                   format TEXT,
                   timestamp INTEGER
        )
    ''')

    # Clear the dataset_version table
    cursor.execute("DELETE FROM dataset_version")

    # Create table columns
    cursor.execute(
        "INSERT INTO dataset_version (version, format, timestamp)" \
        "VALUES (?, ?, ?)",
        (1, "Spack", int(time.time()))
    )

    inserted_packages = 0
    inserted_files = 0
    for package_hash, entry in index.items():
        try:
            package_name = entry["name"]
            version = entry["version"]
            sha256 = entry["sha256"]
        

            tarinfo_path = entry.get("tarinfo_path")
            if not tarinfo_path or not os.path.exists(tarinfo_path):
                print(f"⚠️ Missing tarinfo for: {package_name}-{version}-{sha256}")
                continue

            with open(tarinfo_path, "r") as f:
                file_list = json.load(f)

            package_inserted_or_updated = False

            for file_path in file_list:
                # skipping .spack/ files
                if file_path.startswith(".spack/"):
                    continue

                # Extract file name
                file_name = os.path.basename(file_path)

                # Normalize the file name
                try:
                    normalized = normalize_file_name(file_name)
                    normalized_file_name = str(normalized).lower()
                except Exception as e:
                    print(f"⚠️ Failed to normalize '{file_name}': {e}")
                    normalized_file_name = file_name.lower()

                # Insert into DB
                cursor.execute(
                    '''INSERT OR IGNORE INTO package_files 
                       (file_name, normalized_file_name, file_path, package_name)
                       VALUES (?, ?, ?, ?)''',
                    (file_name, normalized_file_name, file_path, package_name)
                )

                if cursor.rowcount > 0:
                    inserted_files += 1
                    package_inserted_or_updated = True # New row added
                    continue # No need to update - freshly inserted
                #breakpoint()
                # Row already exists - check if any values changed
                cursor.execute(
                    ''' SELECT file_name, normalized_file_name FROM package_files
                        WHERE file_path = ? AND package_name = ?''',
                    (file_path, package_name)
                )
                result = cursor.fetchone()
                if result:
                    existing_file_name, existing_normalized_name = result
                    if (existing_file_name != file_name) or (existing_normalized_name != normalized_file_name):
                        # Something changed - update
                        

                        # Update the row
                        cursor.execute(
                            ''' UPDATE package_files
                                SET file_name = ?, normalized_file_name = ?
                                WHERE file_path = ? AND package_name = ?''',
                            (file_name, normalized_file_name, file_path, package_name)
                        )
                        package_inserted_or_updated = True # A row was updated
            if package_inserted_or_updated:
                inserted_packages += 1
        

        except Exception as e:
            print(f"❌ Failed to insert {package_hash}: {e}")
            continue

    conn.commit()
    conn.close()

    print(f"🎉 Done. Inserted {inserted_files} new files from {inserted_packages} packages into {SQLITE_DB_PATH}")

if __name__ == "__main__":
    build_package_filelist_db()
