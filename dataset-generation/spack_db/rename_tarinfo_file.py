import os
import re

# Path to your tarinfo directory
TARINFO_DIR = "cache/tarinfo"

# Updated regex: match <prefix>-<64-char-sha256>.json
pattern = re.compile(r"^(.*)-([a-f0-9]{64})\.json$")

# Counter
renamed = 0
skipped = 0

for filename in os.listdir(TARINFO_DIR):
    match = pattern.match(filename)
    if match:
        sha256_hash = match.group(2)
        new_filename = f"{sha256_hash}.json"

        old_path = os.path.join(TARINFO_DIR, filename)
        new_path = os.path.join(TARINFO_DIR, new_filename)

        # Skip if target file already exists
        if os.path.exists(new_path):
            print(f"⚠️ Skipping {filename} (target {new_filename} already exists)")
            skipped += 1
            continue

        os.rename(old_path, new_path)
        renamed += 1
    else:
        print(f"❓ Skipping non-matching file: {filename}")
        skipped += 1

print(f"\n✅ Done! Renamed {renamed} files. Skipped {skipped} files.")
