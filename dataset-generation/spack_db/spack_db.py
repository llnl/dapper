# json library used to load json file
import json 

# requests used to download URL automatically
import requests

# io for reading and writing streams such as text, binary, and raw data
import io

# for reading and writing tar archives
import tarfile

# to quiet warnings
import warnings
warnings.filterwarnings("ignore")

# os to create a cache folder - disk-based caching
import os

# tempfile and shutil for index temp file
import tempfile
import shutil

from pathlib import Path

# Configuration

# spack.index.db.json maps each package back to the packagename, version, SHA256hash, 
    # path to manifest json file, path to tarinfo file list
INDEX_FILE = "cache/spack.index.db.json"  

# MANIFEST_DIR is the source of metadata per package used in master index
MANIFEST_DIR = "cache/manifest" 

# TARINFO_DIR contains the extracted list of files from each binary tarball
# this will be used in making the SQLite database without having to reprocess the tarballs again
TARINFO_DIR = "cache/tarinfo" 

# BINARY_CACHE_DIR contains the downloaded tarballs temporarily
# the file is deleted after processing. 
BINARY_CACHE_DIR = "cache/binary_packages" 
TIMEOUT_LOG_FILE = "cache/timeouts.txt"

# checkpoint to safely stop the script at any time
# progress.txt saves the spec_manifest hash
CHECKPOINT_FILE = "progress.txt" 

# file to track all the manifest files that were unable to download
SKIPPED_MANIFESTS_FILE = "cache/skipped_manifests.txt"
MALFORMED_MANIFESTS_FILE = "cache/malformed_manifests.txt"
MISSING_TARBALL_HASH_FILE = "cache/missing_tarballs.txt"
SHARED_TARBALL_HASH_FILE = "cache/shared_tarballs.txt"
FAILED_TARBALL_DOWNLOAD_FILE = "cache/failed_tarball_downloads.txt"

# create cache directories for faster download
os.makedirs(MANIFEST_DIR, exist_ok=True)
os.makedirs(TARINFO_DIR, exist_ok = True)
os.makedirs(BINARY_CACHE_DIR, exist_ok = True)

# Normalize any filesystem path to a forward-slash (POSIX) string for JSON storage.
def _to_posix(p:str) -> str:
    return Path(p).as_posix()

def _to_posix(p: str) -> str:
    return Path(p).as_posix()

# look for index if it exists to add info to
def load_index(): 
    # if the index_file exists, read it
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r") as f:
            # return as a dictionary for easy manipulation and JSON formatting
            return json.load(f)
    # if the index does not exist, an empty dictionary is returned
    return {}

# save index
def save_index(index):                                  

    # create a backup of the previous index
    if os.path.exists(INDEX_FILE):
        shutil.copy(INDEX_FILE, INDEX_FILE + ".bak")

    # Save to a temp file, then move to replace
    temp_dir = os.path.dirname(INDEX_FILE)
    with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False) as tmp:
        json.dump(index, tmp, indent=2)
        temp_name = tmp.name

    shutil.move(temp_name, INDEX_FILE)

# format for entried added to index
def update_index_entry(index, package_hash, package_value, package_zip_hash): 
    name = package_value['spec']['name']
    version = package_value['spec']['version']
    manifest_filename = f"{name}-{version}-{package_hash}.json"
    tarinfo_filename = f"{package_zip_hash}.json"

    manifest_path_fs = os.path.join(MANIFEST_DIR, manifest_filename)
    tarinfo_path_fs = os.path.join(TARINFO_DIR, tarinfo_filename)

    index[package_hash] = {
        "name": name,
        "version": version,
        "sha256": package_zip_hash,

        # store forward-slash form in JSON for portability
        "manifest_path": _to_posix(manifest_path_fs),
        "tarinfo_path": _to_posix(tarinfo_path_fs),
    }

# load that last saved package hash
def load_checkpoint(): #
    # checks if progress.txt exists
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:

            # read and return last processed package_hash
            # strip removes trailing newline or spaces
            return f.read().strip()
    
    # if the file does not exist, return None
    # if None, start from the beginning
    return None


# saves the last processed manifest package_hash to progress.txt
# if the program is interrupted, the saved package_hash will be 
    # the starting point when rerun
def save_checkpoint(package_hash): #
    with open(CHECKPOINT_FILE, "w") as f:
        f.write(package_hash)


# reading file
def readmyfile(myfile):
    try: 
        with open(myfile, 'r') as file:
            # database is the spack database json, within the spack build cache
            db = json.load(file) # 8.6 seconds to read in large json file

            # returns database
            return db
        
    except FileNotFoundError:
        print(f"Error: The file '{myfile}' not found.")
    except Exception as e:
        print(f"Error occured in readmyfile: {e}")


# make the spec manifest downloadable URL
def make_spec_manifest_URL(package_hash, package_hash_value): 
# goal is to make a URL that looks like this -> 
# https://binaries.spack.io/develop/v3/manifests/spec/<name_of_package (not the hash)>/<name_of_package>-<version>-<hash>.spec.manifest.json
# example URL for compiler-wrapper:
# https://binaries.spack.io/develop/v3/manifests/spec/compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3.spec.manifest.json
    
    myURL = 'https://binaries.spack.io/develop/v3/manifests/spec/'
    
    # this accesses the package name (not the package hash that we have been using as the package_hash)
    package_name = package_hash_value['spec']['name']

    # this accesses the package name version number
    package_version = package_hash_value['spec']['version']
    
    # package_filename for use later in removal of placeholder directories in the filepath of the tarball
    package_filename = (package_name + '/' + package_name + '-' + package_version 
            + '-' + package_hash)

    # this updates the URL
    myURL += (package_filename + '.spec.manifest.json')
    
    # returns the URL for the spec manifest and package_filename
    return myURL, package_filename  

# automatically download contents from the URL
def download_from_URL(theURL, package, is_spec=True): 

    
    # makes filename
    # Example: 
        # This -> "compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3.spec.manifest.json"
        # is turned into this -> "compiler-wrapper__compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3.spec.manifest.json"
    package_name = package.replace('/','__')

    # if is_spec is true, meaning the file ends with ".spec.manifest.json",
        # then the file is not saved, but the reponse is returned to remove_lines_spec_manifest() for further manipulation
    # if the file ends with .tar.gz
        # then the file is saved in BINARY_CACHE_DIR
    cache_dir = BINARY_CACHE_DIR if not is_spec else None

    # full file path then is:
        # "cache/spec_manifests/compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
        #cache/manifest\\compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3.json
    cached_path = os.path.join(cache_dir, package_name) if cache_dir else None

    if is_spec:
        print(f"downloading manifest for {package_name}")
    else:
        print(f"temporary save location: {_to_posix(cached_path)} for {package_name}")

    #if cache exists, it does not need to be redownloaded
    if cached_path and os.path.exists(cached_path):
        
        print(f"Using cached file: {_to_posix(cached_path)}")
        
        # rb is read binary
        with open(cached_path, "rb") as f:
            return f.read()

    try:
        label = _to_posix(cached_path) if cached_path else f"manifest: {package_name}"
        print(f"trying download for {label}")
        
        # timeout for 60 seconds
        response = requests.get(theURL, timeout=60, verify=False)

        # a response status code is 200 then the request was successful
        # response.status_code of 404 means does not exist
        if response.status_code == 200:
            if cached_path:
                print(f"download successful for {_to_posix(cached_path)}")
                
                # saves to cache if request is successful
                # wb is write binary
                with open(cached_path, "wb") as f:
                    f.write(response.content)
            return response.content

        else:
            # if URL does not exist, skip and move to next package
            print(f"download failed for package: {package_name}\n")
            
            # return None to stop process due to download failing - goes back to run_program function
            return None
    
    except requests.exceptions.Timeout:
        print(f"⏰ Timeout: Skipping package that took too long to download: {package_name}")
            # Append to file immediately
        with open(TIMEOUT_LOG_FILE, "a") as f:
            f.write(f"{package_name}\t{theURL}\n")
        return None
    
    except Exception as e:
        print(f"download_from_URL package {package_name}, error: {e}")

        # return None to stop process due to download failing
        return None

# remove unnecessary lines in file
def remove_lines_spec_manifest(myfile): 

    # Accept bytes or str; extract between first '{' and last '}'
    data = myfile if isinstance(myfile, (bytes, bytearray)) else myfile.encode("utf-8")

    start = data.find(b"{")
    end = data.rfind(b"}")

    if start == -1 or end == -1 or end < start:
        raise ValueError("Malformed manifest: JSON braces not found")
    
    return json.loads(data[start:end+1].decode("utf-8"))

# returns checksum, sha256 hash used to download the binary tarball
def access_spec_manifest_media_type(db): 

    try:
        # get the value for the key 'data' from the db
        # if the key 'data' does not exist, return empty list
        # temp is db['data']
        for temp in db.get('data', []):
            if temp.get('mediaType') == 'application/vnd.spack.install.v2.tar+gzip':

                # the checksum is the sha256 hash
                return temp.get('checksum')
            
    except Exception as e:
        print(f"Error occured in access_spec_manifest_media_type: {e}")
        return None

# uses checksum returned to generate URL for binary tarball download
def make_binary_package_URL(package_zip_hash): 
# example URL for compiler-wrapper binary package:
# https://binaries.spack.io/develop/blobs/sha256/f4/f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351

    myURL = 'https://binaries.spack.io/develop/blobs/sha256/'

    first_byte = package_zip_hash[:2]
    
    myURL = myURL + first_byte + '/' + package_zip_hash

    return myURL

# ensure tarinfo completeness
def write_tarinfo_safely(tarinfo_path, file_list):
    temp_dir = os.path.dirname(tarinfo_path)
    with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False) as tmp:
        json.dump(file_list, tmp, indent=2)
        temp_name = tmp.name
    shutil.move(temp_name, tarinfo_path)


# ensure manifest completeness
def write_manifest_safely(manifest_path, manifest_data):
    temp_dir = os.path.dirname(manifest_path)
    with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False) as tmp:
        json.dump(manifest_data, tmp, indent=2)
        temp_name = tmp.name
    shutil.move(temp_name, manifest_path)

# using python tarfile module io module to list all the files in the downloaded tarball 
# myfile is the tar_file response.content and the package is the hash we will split by
# package is the package name and the version
def read_binary_package(myfile, package, package_zip_hash): 
    file_list = []
    try:
        with io.BytesIO(myfile) as tar_buffer:
            with tarfile.open(fileobj = tar_buffer, mode="r:*") as tar:
       
                print(f"Files in the tar archive for {package.split('/')[0]}:")
                i = 1
                for member in tar.getmembers():
                    if member.isfile():
                        # member.name for compiler-wrapper is "home/software/spack/__spack_path_placeholder__/
                            # __spack_path_placeholder__/__spack_path_placeholder__/
                            # __spack_path_placeholder__/__spack_path_placeholder__/
                            # __spack_path_placeholder__/__spack_path_placeholder__/__spack_path_placeholder__/
                            # __spack_path_placeh/morepadding/linux-x86_64_v3/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3/
                            # .spack/install_environment.json"
                        # package for compiler-wrapper is "compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
                        clean_path = remove_placeholder_directories(i, member.name, package) 

                        # this will add the files that are in the package to a clean_list
                        if clean_path:
                            file_list.append(clean_path)
                        i += 1
                
    except tarfile.ReadError as e:
        print(f"Error reading tar file: {e}")
        return
    
    name = package.split('/')[0]
    version = package.split('/')[1].split('-')[1]
    
    # saves file names to the tarinfo file
    tarinfo_path = os.path.join(TARINFO_DIR, f"{package_zip_hash}.json")
    write_tarinfo_safely(tarinfo_path, file_list)

    # removes tarball once processed
    tarball_path = os.path.join(BINARY_CACHE_DIR, package.replace('/','__'))
    if os.path.exists(tarball_path):
        os.remove(tarball_path)
    del myfile

# removing the placeholder directories in the file path
def remove_placeholder_directories(i, name, package): 
    # i is the counter for file enumeration
    # name for compiler-wrapper is "home/software/spack/__spack_path_placeholder__/__spack_path_placeholder__/
        # __spack_path_placeholder__/__spack_path_placeholder__/__spack_path_placeholder__/__spack_path_placeholder__/
        # __spack_path_placeholder__/__spack_path_placeholder__/__spack_path_placeh/morepadding/linux-x86_64_v3/
        # compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3/.spack/install_environment.json"
    # package for compiler-wrapper is "compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"

    # updatedpackage_list for compiler-wrapper is "['compiler-wrapper', 'compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3']"
    updatedpackage_list = package.split('/')

    # updatedpackage_name is "compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
    updatedpackage_name = updatedpackage_list[1]

    placeholder = "__spack_path_placeh"
    
    # split by updatedpackage_name
    split_list = name.split(updatedpackage_name)
    
    try:
        if placeholder not in split_list[0]:
            print("split_list", split_list)

        # returns file name without the placeholder path
        elif len(split_list) > 1:
            updatedname = split_list[1][1:]

            print(f"file {i}: ", updatedname)
            return updatedname # return to add to list of files for respective package
            
    except Exception as e:
        print(f"Error in remove_placeholder_directories: {e}")
        return None
    

def print_files(package_hash, package_value, index, existing_tarinfo_files, seen_tarball_hashes): 
    name = package_value['spec']['name']
    version = package_value['spec']['version']
    
    manifest_filename = f"{name}-{version}-{package_hash}.json"
    manifest_path = os.path.join(MANIFEST_DIR, manifest_filename)

    # use existing cleaned manifest if available
    if os.path.exists(manifest_path):
        print(f"Using existing cleaned manifest: {_to_posix(manifest_path)}")
        with open(manifest_path, "r") as f:
            clean_spec_manifest = json.load(f)

        # returns the URL for the spec manifest file and the package_filename
        theURL, package_filename = make_spec_manifest_URL(package_hash, package_value)
    
    else:
        # download if manifest does not exist

        # returns the URL for the spec manifest file and the package_filename
        theURL, package_filename = make_spec_manifest_URL(package_hash, package_value)

        # download the spec manifest json for the package of interest
        temp = download_from_URL(theURL, package_filename, is_spec = True)
        

        # return if URL does not exist
        if temp is None:
            print(f"Could not download manifest: {package_filename} - recording and skipping.\n")
            
            # recording the failed manifest filename and hash
            with open(SKIPPED_MANIFESTS_FILE, "a") as f:
                f.write(f"{package_hash}\n")
            
            # exit print_files function and goes back to run_program function
            return

        print("✅ Loaded cached spec manifest")
    
        try:
            # remove unneccessary lines from downloaded spec manifest
            clean_spec_manifest = remove_lines_spec_manifest(temp)
        except Exception as e:
            print(f"Failed to parse manifest {package_filename}: {e}")
            with open(MALFORMED_MANIFESTS_FILE, "a") as f:
                f.write(f"{package_hash}\t{theURL}\t{type(e).__name__}: {e}\n")
            # also adding it to file for skipping processing at restart
            with open(SKIPPED_MANIFESTS_FILE, "a") as f:
                f.write(f"{package_hash}\n")
            return

        # writes cleaned manifest information to manifest file
        write_manifest_safely(manifest_path, clean_spec_manifest)
        print(f"✅ Manifest safely written: {_to_posix(manifest_path)}")

    # find the mediaType that contains the hash for the package tarball install
    package_zip_hash = access_spec_manifest_media_type(clean_spec_manifest)
    print(f"✅ Extracted zip hash: {package_zip_hash}")

    # if 'data' key was not found in access_spec_manifest_media_type, None is returned and we go back to run_program function
    if package_zip_hash is None:
        print(f"No Tarball hash found in manifest: {package_filename}")

        # Track taht this manifest has no downloadable binary tarball
        with open(MISSING_TARBALL_HASH_FILE, "a") as f:
            f.write(f"{package_hash}\n")
        # go back to run_program function
        return
    
    # track if the tarball hash has already been processed
    if package_zip_hash in seen_tarball_hashes:
        with open(SHARED_TARBALL_HASH_FILE, "a") as f:
            # if this manifest points to a tarball that has already been seen,
                # it will not create a new tarinfo entry
                # it will have a new manifest entry
            f.write(f"{package_hash}\t{package_zip_hash}\n")
    else:
        seen_tarball_hashes.add(package_zip_hash)
        

    expected_tarinfo_hash = package_zip_hash

    if expected_tarinfo_hash in existing_tarinfo_files:
        print(f"✅ Already have tarinfo for {name}-{version}-{expected_tarinfo_hash}, skipping binary download.")
        update_index_entry(index, package_hash, package_value, package_zip_hash)
        save_index(index)
        print(f"✅ Saved Index")
        return
    
    else:

        # make the binary package URL for installing the package
        binary_package_URL = make_binary_package_URL(package_zip_hash)
        print(f"🔗 Downloading binary: {binary_package_URL}")

        # download the binary package file from the generated URL
        tempbinary = download_from_URL(binary_package_URL, package_filename, is_spec = False)
        print("✅ Binary package downloaded")

        if tempbinary is None:

            # Track failed tarball download
            with open(FAILED_TARBALL_DOWNLOAD_FILE, "a") as f:
                f.write(f"{package_filename}: manifest hash: {package_hash}, tarball hash: {package_zip_hash}\n")
            
            return

        # read the binary package
        read_binary_package(tempbinary, package_filename, package_zip_hash)
        print("✅ Finished reading binary package")
        update_index_entry(index, package_hash, package_value, package_zip_hash)
        save_index(index)
        print(f"Updated Index with {package_filename}-{package_zip_hash}")
        save_checkpoint(package_hash)
        print(f"✅ Saved Index")

# program dispatcher
def run_program(package_hash, database, index, existing_tarinfo_files, seen_tarball_hashes): 
    installs = database['database']['installs']

    # gets installs key value, aka name of package, version, etc.
    package_value = installs[package_hash]
    print_files(package_hash, package_value, index, existing_tarinfo_files, seen_tarball_hashes)


def main():
    #file_name = "myMedjson.json"
    # file_name = "myjson.json"
    # file_name = 'Med_w_compilerwrapper_packages_at_end.json'
    file_name = "e2a6969c742c8ee33deba2d210ce2243cd3941c6553a3ffc53780ac6463537a9"

    database = readmyfile(file_name)

    # load list of previously skipped manifest hashes
    if os.path.exists(SKIPPED_MANIFESTS_FILE):
        with open(SKIPPED_MANIFESTS_FILE, "r") as f:
            skipped_hashes = set(line.strip() for line in f)
    
    else:
        skipped_hashes = set()

    # load manifests that are missing binary tarball hashes (e.g. mediaType not found)
    if os.path.exists(MISSING_TARBALL_HASH_FILE):
        with open(MISSING_TARBALL_HASH_FILE, "r") as f:
            missing_tarball_hashes = set(line.strip() for line in f)

    else:
        missing_tarball_hashes = set()

    # load tarballs that were not downloadable
    if os.path.exists(FAILED_TARBALL_DOWNLOAD_FILE):
        with open(FAILED_TARBALL_DOWNLOAD_FILE, "r") as f:
            failed_tarball_hashes = set(
                line.strip().split("tarball hash: ")[-1] for line in f if "tarball hash:" in line
            )
    else:
        failed_tarball_hashes = set()
   

    # lists install keys
    install_keys = list(database['database']['installs'])
    num_keys = len(install_keys)

    # load last processed package hash from checkpoint from cache if it exists
    last_processed = load_checkpoint()
    index = load_index()
    skip = True if last_processed else False

    existing_tarinfo_files = {
        Path(fname).stem
        for fname in os.listdir(TARINFO_DIR)
        if fname.endswith(".json")
    }

    # track already-processed tarball hashes to find shared ones
    seen_tarball_hashes = set()

    SAVE_INTERVAL = 50

    print("Starting...Will skip packages already fully processed.")

    try:
        for i, package_hash in enumerate(install_keys):

            # skip if package_hash is in the skipped manifests file
            if package_hash in skipped_hashes:
                continue

            # skip if manifest had no usable tarball
            if package_hash in missing_tarball_hashes:
                continue

            if package_hash in index:
                entry = index[package_hash]
                manifest_path = entry.get("manifest_path", "")
                tarinfo_path = entry.get("tarinfo_path", "")
                tarball_hash = entry.get("sha256", "")

                manifest_exists = bool(manifest_path) and Path(manifest_path).exists()
                tarinfo_exists = bool(tarinfo_path) and Path(tarinfo_path).exists()
                
                if manifest_exists and tarinfo_exists:
                    
                    continue
                
                # if tarball previously failed, skip retrying it
                if tarball_hash in failed_tarball_hashes:
                    print(f"🚫 Skipping manifest with previously failed tarball download: {package_hash}")
                    continue


            print(f"📦 package {i + 1} out of {num_keys} packages\n")

            run_program(package_hash, database, index, existing_tarinfo_files, seen_tarball_hashes)

            # Save checkpoint and index every N packages
            if (i + 1) % SAVE_INTERVAL == 0:
                save_checkpoint(package_hash)
                save_index(index)
                print(f"Saved checkpoint and index at package {i + 1}")

    except KeyboardInterrupt:
        save_checkpoint(package_hash)
        save_index(index)
        print("\n🛑 Interrupted. Progress saved.")

        if last_processed:
            val = database['database']['installs'].get(last_processed)
            if val:
                name = val['spec']['name']
                version = val['spec']['version']

                print(f"Last checkpoint was: {name}-{version}, {last_processed}")
            else:
                print(f"Checkpoint not found in current file: {last_processed}")
                print(f"file may have changed since the last run")
    finally:
        save_checkpoint(package_hash)
        save_index(index)
        print("\n🎊 Complete (or safely stopped). Script will resume where it left off.")

if __name__ == "__main__":
    main()