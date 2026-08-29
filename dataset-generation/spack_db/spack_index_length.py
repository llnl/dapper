import json

# Path to your index file
INDEX_FILE = "cache/spack.index.db.json"

def main():
    try:
        with open(INDEX_FILE, "r") as f:
            index = json.load(f)
        print(f"✅ Number of entries in index: {len(index)}")
    except FileNotFoundError:
        print(f"❌ File not found: {INDEX_FILE}")
    except json.JSONDecodeError:
        print(f"❌ Failed to parse JSON. The file may be corrupted.")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")

if __name__ == "__main__":
    main()