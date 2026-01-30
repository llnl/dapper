package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
)

// Pagination info in hub.docker.com API requests: https://hub.docker.com/v2/
type Pagination struct {
	Count    int         `json:"count"`
	Next     string      `json:"next"`
	Previous interface{} `json:"previous"`
}

// Used in Repository struct
type Category struct {
	Name string `json:"name"`
	Slug string `json:"slug"`
}

// Objects in results list returned by https://hub.docker.com/v2/repositories/library/
type Repository struct {
	Affiliation       string     `json:"affiliation"`
	Categories        []Category `json:"categories"`
	ContentTypes      []string   `json:"content_types"`
	DateRegistered    string     `json:"date_registered"`
	Description       string     `json:"description"`
	IsPrivate         bool       `json:"is_private"`
	LastModified      string     `json:"last_modified"`
	LastUpdated       string     `json:"last_updated"`
	MediaTypes        []string   `json:"media_types"`
	Name              string     `json:"name"`
	Namespace         string     `json:"namespace"`
	PullCount         int64      `json:"pull_count"`
	RepositoryType    string     `json:"repository_type"`
	StarCount         int        `json:"star_count"`
	Status            int        `json:"status"`
	StatusDescription string     `json:"status_description"`
	StorageSize       int64      `json:"storage_size"`
}

func fetchData(url string, retries int, backoffFactor float64) (map[string]interface{}, error) {
	for i := 0; i < retries; i++ {
		resp, err := http.Get(url)
		if err == nil && resp.StatusCode == http.StatusOK {
			defer resp.Body.Close()
			var data map[string]interface{}
			if err = json.NewDecoder(resp.Body).Decode(&data); err == nil {
				return data, nil
			}
		}
		log.Printf("Request failed: %v. Retrying in %f seconds...", err, backoffFactor*float64(int(1<<i)))
		time.Sleep(time.Duration(backoffFactor * float64(int(1<<i)) * float64(time.Second)))
	}
	return nil, errors.New("failed to fetch data after retries")
}

func fetchAllPages(url string) []map[string]interface{} {
	var results []map[string]interface{}
	for url != "" {
		data, err := fetchData(url, 5, 0.3)
		if err != nil {
			log.Printf("Error fetching data: %v", err)
			break
		}
		items, ok := data["results"].([]interface{})
		if !ok {
			break
		}
		for _, item := range items {
			m, _ := item.(map[string]interface{})
			results = append(results, m)
		}
		if nextURL, ok := data["next"].(string); ok {
			url = nextURL
		} else {
			url = ""
		}
	}
	return results
}

func saveJSON(data interface{}, filePath string) error {
	bytes, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filePath, bytes, 0644)
}

func loadOrFetchData(filePath, baseURL string, updateCacheFunc func([]map[string]interface{}, []map[string]interface{}) []map[string]interface{}) ([]map[string]interface{}, error) {
	var data []map[string]interface{}
	if _, err := os.Stat(filePath); err == nil {
		bytes, err := os.ReadFile(filePath)
		if err != nil {
			return nil, fmt.Errorf("failed to read existing JSON file: %v", err)
		}
		if err := json.Unmarshal(bytes, &data); err != nil {
			return nil, fmt.Errorf("failed to unmarshal existing JSON file: %v", err)
		}
		log.Printf("Loaded data from existing JSON file: %s", filePath)
		if updateCacheFunc != nil {
			newData := fetchAllPages(fmt.Sprintf("%s?page_size=100", baseURL))
			data = updateCacheFunc(data, newData)
			// Commented out to avoid updates while testing updateCacheFunc
			// if err := saveJSON(data, filePath); err != nil {
			// 	log.Printf("Failed to save updated JSON: %v", err)
			// }
		}
	} else {
		data = fetchAllPages(fmt.Sprintf("%s?page_size=100", baseURL))
		if err := saveJSON(data, filePath); err != nil {
			log.Printf("Failed to save JSON: %v", err)
		}
		log.Printf("Fetched data from web API: %s", baseURL)
	}
	return data, nil
}

func fetchManifestList(puller *remote.Puller, ref string) (map[string]interface{}, error) {
	// This function is meant to get the raw list of images for a given tag
	imageRef, err := name.ParseReference(ref)
	if err != nil {
		panic(err)
	}

	// For "inactive" tags that use an older manifest schema, this should return a list of image digests
	// for all platforms that will work as expected for fetching later
	img, err := remote.Get(imageRef, remote.Reuse(puller))
	if err != nil {
		return nil, err
	}
	manifest, err := img.RawManifest()
	if err != nil {
		panic(err)
	}

	// Use the returned information to fetch the config for that image
	var manifestData map[string]interface{}
	if err := json.Unmarshal(manifest, &manifestData); err != nil {
		panic(err)
	}

	return manifestData, nil
}

func fetchRawManifest(puller *remote.Puller, repo, ref string) ([]byte, error) {
	// This function is meant to get the raw list of images for a given tag/digest
	imageRef, err := name.ParseReference(fmt.Sprintf("%s%s", repo, ref))
	if err != nil {
		panic(err)
	}

	// Mainly
	img, err := remote.Get(imageRef, remote.Reuse(puller))
	if err != nil {
		return nil, err
	}
	manifest, err := img.RawManifest()
	if err != nil {
		panic(err)
	}

	return manifest, nil
}

func fetchAndSaveConfig(puller *remote.Puller, manifestDir, configDir, repo_name, tag_name, digest, tagMediaType string) ([]byte, error) {
	// Try using mirrors first to save on Docker Hub rate limits
	// - public.ecr.aws/docker/library/ # had 180245 manifests, and 180207 config files mirrored from Docker Hub
	// - mirror.gcr.io/library/ (or mirror.gcr.io)
	// rate limit for docker hub: 100 pulls per 6 hours anonymous per IP, 25k/mo authenticated on Pro plan (might need to get higher tier?)
	// rate limit for AWS is 1 per second (quickly stops working if you exceed it, within 15sec if no limiting)
	// rate limit for GCR is around 50 per minute?
	//time.Sleep(500 * time.Millisecond) // around 800ms seems to stay just below the AWS rate limit; GCR rate limit is weird... maybe sometimes passes request through to Docker Hub with very strict rate limit (even fails with 1800ms)?
	var imageRefStr string
	var manifestSaveFile string
	if tagMediaType == "application/vnd.docker.container.image.v1+json" || tagMediaType == "application/octet-stream" {
		// Use the tag name if the mediaType is for an image (the digest we have for the tag is of the config, and hard to fetch on its own)
		imageRefStr = fmt.Sprintf("%s%s", repo_name, tag_name)
		manifestSaveFile = fmt.Sprintf("%s%s.json", manifestDir, imageRefStr)
	} else {
		// Use the digest to fetch a specific, since the mediaType should be for something that is a list of manifests
		imageRefStr = fmt.Sprintf("%s@%s", repo_name, digest)
		manifestSaveFile = fmt.Sprintf("%s%s.json", manifestDir, digest)
	}
	imageRef, err := name.ParseReference(imageRefStr)
	if err != nil {
		panic(err)
	}

	// Query the catalog for the image manifest (type: "application/vnd.oci.image.manifest.v1+json")
	// This gives digests for the compressed layers, and the digest for the config file
	img, err := remote.Image(imageRef, remote.Reuse(puller)) //, remote.WithPlatform(v1.Platform{OS: "linux", Architecture: "amd64"}))
	if err != nil {
		return nil, err
	}

	manifest, err := img.RawManifest()
	if err != nil {
		panic(err)
	}

	// Save the manifest
	if err := os.WriteFile(manifestSaveFile, manifest, 0644); err != nil {
		panic(err)
	}

	// Use the returned information to fetch the config for that image
	var manifestData map[string]interface{}
	if err := json.Unmarshal(manifest, &manifestData); err != nil {
		panic(err)
	}

	configDigest, ok := manifestData["config"].(map[string]interface{})["digest"].(string)
	if !ok {
		return nil, errors.New("failed to get config digest from manifest data")
	}

	conf, err := img.RawConfigFile()
	if err != nil {
		return nil, err
	}

	// Save the config
	if err := os.WriteFile(fmt.Sprintf("%s%s.json", configDir, configDigest), conf, 0644); err != nil {
		panic(err)
	}
	return conf, nil
}

func loadExistingConfig(manifestDir, configDir, repo, tag, digest, tagMediaType string) ([]byte, error) {
	// First, check if the tagMediaType is application/vnd.docker.container.image.v1+json -- if it is, the digest is for the config, and we saved the manifest by tag name
	// Then, check if the manifest for the given digest or tag is already saved - if it is, load it and grab the digest for the config file
	// Then check if the config file digest is already saved -- load and return it if so
	// If nothing is found, then return nil for data and no error to signal that the remote needs to be queried
	var manifestFilePath string
	// Possible TODO is check if manifest file with repo:tag is saved for this case, and load digest from that (which should be the same)
	if tagMediaType == "application/vnd.docker.container.image.v1+json" || tagMediaType == "application/octet-stream" {
		manifestFilePath = fmt.Sprintf("%s%s%s.json", manifestDir, repo, tag)
	} else {
		manifestFilePath = fmt.Sprintf("%s%s.json", manifestDir, digest)
	}
	if _, err := os.Stat(manifestFilePath); err == nil {
		bytes, err := os.ReadFile(manifestFilePath)
		if err != nil {
			return nil, fmt.Errorf("failed to read existing JSON file: %v", err)
		}
		var data map[string]interface{}
		if err := json.Unmarshal(bytes, &data); err != nil {
			return nil, fmt.Errorf("failed to unmarshal existing JSON file: %v", err)
		}
		log.Printf("Loaded manifest from existing JSON file: %s", manifestFilePath)

		if schemaVersion, ok := data["schemaVersion"].(float64); ok && schemaVersion == 1 {
			log.Printf("Loaded manifest has schemaVersion 1 (it is the config, nothing else to get): %s", manifestFilePath)
			return bytes, nil
		}

		log.Printf("Manifest contents: %s", data)
		// Get the config digest from the manifest
		var ok bool
		configDigest, ok := data["config"].(map[string]interface{})["digest"].(string)
		if !ok {
			return nil, errors.New("failed to get config digest from manifest data")
		}
		configFilePath := fmt.Sprintf("%s%s.json", configDir, configDigest)
		if _, err := os.Stat(configFilePath); err == nil {
			bytes, err := os.ReadFile(configFilePath)
			if err != nil {
				return nil, fmt.Errorf("failed to read existing JSON file: %v", err)
			}
			log.Printf("Loaded config from existing JSON file: %s", configFilePath)
			return bytes, nil
		}
	}
	return nil, nil
}

func loadOrFetchConfig(puller *remote.Puller, manifestDir, configDir, repo_name, tag_name, digest, tagMediaType string) ([]byte, error) {
	// Fetching is done using digests to avoid having to deal with specifying platform/os tags when making a query
	// The image manifest (type: "application/vnd.oci.image.manifest.v1+json") has digests for compressed layers and the config file
	// The config file digest is used to fetch the config file which has a rootfs entry with a list of sha256 digests for the uncompressed layers
	// The uncompressed layers are what we actually want for identifying "well-known" images

	// First, check if the manifest and config files already exist in our local cache
	data, err := loadExistingConfig(manifestDir, configDir, repo_name, tag_name, digest, tagMediaType)
	if err == nil && data != nil {
		return data, nil
	}
	// No cached copy if we made it here, query the remote repository
	log.Printf("Fetching image manifest and config from remote: %s@%s", repo_name, digest)
	return fetchAndSaveConfig(puller, manifestDir, configDir, repo_name, tag_name, digest, tagMediaType)
}

func printTagData(tagData map[string][]map[string]interface{}) {
	for repo_name, tags := range tagData {
		for _, tag := range tags {
			if images, ok := tag["images"].([]interface{}); ok {
				tag_name, tagNameOk := tag["name"].(string)
				if tagNameOk {
					tag_name = fmt.Sprintf(":%s", tag_name)
				} else {
					tag_name = ""
				}
				for _, image := range images {
					imageMap, _ := image.(map[string]interface{})
					architecture, archOk := imageMap["architecture"].(string)
					os, osOk := imageMap["os"].(string)
					if archOk && osOk && architecture != "unknown" && os != "unknown" {
						digest, _ := imageMap["digest"].(string)
						variant, variantOk := imageMap["variant"].(string)
						if variantOk && variant != "" {
							fmt.Printf("Digest: %s (%s%s %s/%s %s)\n", digest, repo_name, tag_name, os, architecture, variant)
						} else {
							fmt.Printf("Digest: %s (%s%s %s/%s)\n", digest, repo_name, tag_name, os, architecture)
						}
					}
				}
			}
		}
	}
}

func loadUnsupportedManifests(filePath string) (map[string]map[string]bool, error) {
	unsupportedManifests := make(map[string]map[string]bool)
	file, err := os.Open(filePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		parts := strings.SplitN(line, "@", 2)
		if len(parts) < 2 {
			log.Printf("Failed to split line: %s", line)
			continue
		}
		repoTag := parts[0]
		digest := parts[1]
		if _, exists := unsupportedManifests[repoTag]; !exists {
			unsupportedManifests[repoTag] = make(map[string]bool)
		}
		unsupportedManifests[repoTag][digest] = true
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return unsupportedManifests, nil
}

func getManifestPlatformInfo(manifestMap map[string]interface{}) (string, *string, string, *string, bool) {
	// Returns architecture, variant, os, os_version, and a boolean indicating if all fields were found
	if platform, ok := manifestMap["platform"].(map[string]interface{}); ok {
		architecture, archOk := platform["architecture"].(string)
		os, osOk := platform["os"].(string)
		variant, variantOk := platform["variant"].(string)
		// Windows seems to often use os.version field (but null on Linux) -- our JSON files save it as os_version
		osVersion, osVersionOk := platform["os.version"].(string)
		if archOk && osOk {
			var variantPtr *string
			if variantOk {
				variantPtr = &variant
			}
			var osVersionPtr *string
			if osVersionOk {
				osVersionPtr = &osVersion
			}
			return architecture, variantPtr, os, osVersionPtr, true
		}
	}
	return "", nil, "", nil, false
}

func updateRepositoriesCache(cachedData, newData []map[string]interface{}) []map[string]interface{} {
	// Used to updated cached info on what repositories are available
	lookup := make(map[string]map[string]interface{})
	//fmt.Println("----Cached Data Keys----")
	for _, c := range cachedData {
		key := fmt.Sprintf("%s:%s", c["namespace"], c["name"])
		lookup[key] = c
		//fmt.Printf("%s\n", key)
	}
	//fmt.Println("----New Data----")
	for _, n := range newData {
		key := fmt.Sprintf("%s:%s", n["namespace"], n["name"])
		if cached, ok := lookup[key]; ok {
			// Update fields from newData
			if cached["status"] != n["status"] || cached["repository_type"] != n["repository_type"] || cached["status_description"] != n["status_description"] {
				fmt.Printf("[WARNING] Repo Status Change: %s\n", key)
				fmt.Printf("%s -> %s\n%s -> %s\n%s -> %s\n", cached["status"], n["status"], cached["repository_type"], n["repository_type"], cached["status_description"], n["status_description"])
			}
			if cached["last_updated"] != n["last_updated"] {
				//fmt.Printf("Updating: %s\n", key)
				// Currently just updating all of the fields, but only a subset should ever regularly change
				cached["affiliation"] = n["affiliation"]
				cached["categories"] = n["categories"]
				cached["content_types"] = n["content_types"]
				cached["date_registered"] = n["date_registered"]
				cached["description"] = n["description"]
				cached["is_private"] = n["is_private"]
				cached["last_modified"] = n["last_modified"]
				cached["last_updated"] = n["last_updated"]
				cached["media_types"] = n["media_types"]
				cached["pull_count"] = n["pull_count"]
				cached["repository_type"] = n["repository_type"]
				cached["star_count"] = n["star_count"]
				cached["status"] = n["status"]
				cached["status_description"] = n["status_description"]
				cached["storage_size"] = n["storage_size"]
			}
		} else {
			//fmt.Printf("Adding: %s\n", key)
			cachedData = append(cachedData, n)
		}
	}
	return cachedData
}

func updateTagsCache(cachedData, newData []map[string]interface{}) []map[string]interface{} {
	lookup := make(map[string]map[string]interface{})
	for _, c := range cachedData {
		if name, ok := c["name"].(string); ok {
			lookup[name] = c
		}
	}
	for _, n := range newData {
		name, ok := n["name"].(string)
		if !ok {
			continue
		}
		if cached, ok := lookup[name]; ok {
			if cached["last_updated"] != n["last_updated"] {
				// Update all fields
				for k, v := range n {
					cached[k] = v
				}
			}
		} else {
			cachedData = append(cachedData, n)
		}
	}
	return cachedData
}

func checkRepoNeedsUpdate(repo map[string]interface{}, tagsFilePath string) bool {
	lastUpdatedStr, ok := repo["last_updated"].(string)
	if !ok {
		return true // No last_updated info, assume update needed
	}

	// Parse repo last updated time
	repoTime, err := time.Parse(time.RFC3339Nano, lastUpdatedStr)
	if err != nil {
		// Try without Nano if it fails, although RFC3339Nano covers standard RFC3339
		repoTime, err = time.Parse(time.RFC3339, lastUpdatedStr)
		if err != nil {
			log.Printf("Failed to parse last_updated: %s", lastUpdatedStr)
			return true
		}
	}

	fileInfo, err := os.Stat(tagsFilePath)
	if os.IsNotExist(err) {
		return true
	}
	if err != nil {
		return true
	}

	return repoTime.After(fileInfo.ModTime())
}

func main() {
	saveDir := flag.String("saveDir", "", "Directory to save fetched JSON/config/manifest files")
	getUnsupportedManifests := flag.Bool("getUnsupportedManifests", false, "Read and use the failed fetches file")
	updateCache := flag.Bool("updateCache", true, "If files for the repositories catalog and manifests exist, update them with new repositories or tags that are newer")
	flag.Parse()

	if *saveDir != "" {
		if (*saveDir)[len(*saveDir)-1] != '/' {
			*saveDir += "/"
		}
		if err := os.MkdirAll(*saveDir, 0755); err != nil {
			log.Fatalf("Failed to create directory: %v", err)
		}
	}

	var unsupportedManifests map[string]map[string]bool
	if *getUnsupportedManifests {
		var err error
		unsupportedManifests, err = loadUnsupportedManifests(fmt.Sprintf("%sunsupported_manifest.txt", *saveDir))
		if err != nil {
			log.Fatalf("Failed to load failed fetches: %v", err)
		}
	} else {
		unsupportedManifests = make(map[string]map[string]bool)
	}

	// Make a reuseable puller for fetching images from e.g. Docker Hub which has a higher rate limit when authenticated
	puller, _ := remote.NewPuller(remote.WithAuthFromKeychain(authn.DefaultKeychain))

	ticker := time.NewTicker(30 * time.Minute)
	defer ticker.Stop()

	go func() {
		for range ticker.C {
			puller, _ = remote.NewPuller(remote.WithAuthFromKeychain(authn.DefaultKeychain))
			log.Println("Refreshed puller")
		}
	}()

	// Get repositories list first
	repositoriesOutFile := fmt.Sprintf("%srepositories.json", *saveDir)

	baseURL := "https://hub.docker.com/v2/repositories/library"

	var cacheFunc func([]map[string]interface{}, []map[string]interface{}) []map[string]interface{}
	if *updateCache {
		cacheFunc = updateRepositoriesCache
	} else {
		cacheFunc = nil
	}
	repositories, err := loadOrFetchData(repositoriesOutFile, baseURL, cacheFunc)
	if err != nil {
		log.Fatalf("Failed to load or fetch repositories: %v", err)
	}

	// Next, get the tags for each repository
	// The info in the images list for each tag is of type: application/vnd.oci.image.index.v1+json
	// Fetching from the registry instead of the web API gives an additional list of annotations
	// Not necessary, but interesting fields include:
	// - "org.opencontainers.image.base.name": "scratch",
	// - "org.opencontainers.image.revision": "907e0f82e65afd01dae07774db9c70fb73c78eb2",
	// - "org.opencontainers.image.source": "https:\/\/github.com\/docker-library\/busybox.git",
	// - "org.opencontainers.image.version": "1.37.0-glibc"
	// The version seems to be the tag name the image was built with (so the real version of the "latest" tag)
	// It would be a lot of queries to call this individual for each tag though... backfill later if desired
	tagsDir := fmt.Sprintf("%stags/", *saveDir)
	if err := os.MkdirAll(tagsDir, 0755); err != nil {
		log.Fatalf("Failed to create directory: %v", err)
	}

	// Open a file for writing a list of things that fail to fetch
	failedBackFillFile, err := os.OpenFile(fmt.Sprintf("%sbackfill_failures.txt", *saveDir), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("Failed to open failed fetches file: %v", err)
	}
	defer failedBackFillFile.Close()

	tagData := make(map[string][]map[string]interface{})
	i := 0
	// weird corner cases in data fetched:
	// - "kong:0.10.2" tag is missing media_type field (fixed by modifying tags JSON file for kong to have image media type)
	// - media types range from being manifest lists/indexes, to a single image manifest (for older tags) or application/octet-stream (for even older tags)
	// - unsupported MediaType: "application/vnd.docker.distribution.manifest.v1+prettyjws" for some tags (fixed by fetching raw manifest, schemaVersion should == 1) (e.g. python:3.4.9-alpine3.9 for 386 linux)
	for _, repo := range repositories {
		if _, ok := repo["name"]; !ok {
			log.Printf("[WARNING] No name in repo: %v", repo)
		}
		repo_name := repo["name"].(string)

		tagsOutFile := fmt.Sprintf("%s%s.json", tagsDir, repo_name)
		var tagsUpdateFunc func([]map[string]interface{}, []map[string]interface{}) []map[string]interface{}
		if *updateCache && checkRepoNeedsUpdate(repo, tagsOutFile) {
			tagsUpdateFunc = updateTagsCache
		}
		tags, err := loadOrFetchData(tagsOutFile, fmt.Sprintf("%s/%s/tags", baseURL, repo_name), tagsUpdateFunc)
		for _, tag := range tags {
			tag_name, tagNameOk := tag["name"].(string)
			tag_status, tagStatusOk := tag["tag_status"].(string)
			if !tagNameOk || !tagStatusOk {
				continue
			}
			// TODO there may be some tags we want to fetch where one of arch/os is null, but the other is present... check dataset to see what's up with these ones

			// This is a bit of a hack -- basically, the website returns digests that don't work for getting more data
			// the unsupported manifests option looks at the list output after trying to fetch all tags for ones that got
			// a MANIFEST_SCHEMA_UNSUPPORTED error, and then tries to get digests for updated manifests for those tags
			// and then re-saving the tag data files with the new digets. Future (regular?) updates should recognize
			// when tag data exists already and only add data for tags that is newer than what is already saved, so
			// hopefully running again with this option is never needed.
			// tl;dr if unsupported_manifest.txt has any entries, run with this option to try to get new digests, then re-run without
			// to get fill in the rest of the data. backfill_failures.txt has any tags that failed to update in it.
			// In general this happens for tags marked as "inactive", so might as well make sure all digests get updated too.
			// TODO eventual refactor to make this only check newly fetch tags rather than all loaded ones
			if _, exists := unsupportedManifests[repo_name+":"+tag_name]; exists || (tag_status == "inactive" && *getUnsupportedManifests) {
				newManifests, _ := fetchManifestList(puller, fmt.Sprintf("%s:%s", repo_name, tag_name))
				if newManifests != nil {
					log.Printf("Got new manifest for %s:%s", repo_name, tag_name)
					if manifests, ok := newManifests["manifests"].([]interface{}); ok {
						// If there are manifests for more platforms now than before, add them to the list of images for the tag
						if len(manifests) != len(tag["images"].([]interface{})) {
							if len(manifests) < len(tag["images"].([]interface{})) {
								if _, err := failedBackFillFile.WriteString(fmt.Sprintf("LESS NEW MANIFESTS THAN ORIGINALLY %s:%s\n", repo_name, tag_name)); err != nil {
									log.Printf("Failed to write to back fill failures file: %v", err)
								}
							}
							for _, manifest := range manifests {
								if manifestMap, ok := manifest.(map[string]interface{}); ok {
									digest, digestOk := manifestMap["digest"].(string)
									architecture, variant, os, os_version, platformOk := getManifestPlatformInfo(manifestMap)
									if platformOk && digestOk {
										found := false
										for _, image := range tag["images"].([]interface{}) {
											imageMap, _ := image.(map[string]interface{})
											if imageMap["architecture"] == architecture &&
												imageMap["os"] == os &&
												((variant == nil && imageMap["variant"] == nil) || (variant != nil && imageMap["variant"] == *variant)) &&
												((os_version == nil && imageMap["os_version"] == nil) || (os_version != nil && imageMap["os_version"] == *os_version)) {
												found = true
												break
											}
										}
										if !found {
											variantStr := ""
											if variant != nil {
												variantStr = *variant
											}
											osVersionStr := ""
											if os_version != nil {
												osVersionStr = *os_version
											}
											log.Printf("New manifest found for %s:%s: OS=%s, OS Version=%s, Architecture=%s, Variant=%s, Digest=%s", repo_name, tag_name, os, osVersionStr, architecture, variantStr, digest)
											imageMap := map[string]interface{}{
												"architecture": architecture,
												"os":           os,
												"os_version":   os_version,
												"digest":       digest,
												"status":       "",
											}
											if variant != nil {
												imageMap["variant"] = *variant
											} else {
												imageMap["variant"] = nil
											}
											tag["images"] = append(tag["images"].([]interface{}), imageMap)
										}
									}
								}
							}
						}
						// Now update the digests based on what we got from the actual container registry
						for _, manifest := range manifests {
							if manifestMap, ok := manifest.(map[string]interface{}); ok {
								digest, digestOk := manifestMap["digest"].(string)
								architecture, variant, os, os_version, platformOk := getManifestPlatformInfo(manifestMap)
								if platformOk && digestOk {
									notFound := true
									for _, image := range tag["images"].([]interface{}) {
										imageMap, _ := image.(map[string]interface{})
										if imageMap["architecture"] == architecture && imageMap["os"] == os && ((variant == nil && imageMap["variant"] == nil) || (variant != nil && imageMap["variant"] == *variant)) && ((os_version == nil && imageMap["os_version"] == nil) || (os_version != nil && imageMap["os_version"] == *os_version)) {
											imageMap["digest"] = digest
											notFound = false
											break
										}
									}
									if notFound {
										if _, err := failedBackFillFile.WriteString(fmt.Sprintf("MATCHING IMAGE NOT FOUND FOR %s:%s@%s\n", repo_name, tag_name, digest)); err != nil {
											log.Printf("Failed to write to back fill failures file: %v", err)
										}
									}
								} else {
									if _, err := failedBackFillFile.WriteString(fmt.Sprintf("FAILED GET OS/ARCH/DIGEST %s:%s@%s\n", repo_name, tag_name, digest)); err != nil {
										log.Printf("Failed to write to back fill failures file: %v", err)
									}
								}
							}
						}
					}
				}
			}
		}
		if err := saveJSON(tags, tagsOutFile); err != nil {
			log.Printf("Failed to save JSON: %v", err)
		}
		if err != nil {
			log.Printf("Failed to load or fetch tags for repository %s: %v", repo_name, err)
			continue
		}
		tagData[repo_name] = tags
		log.Printf("Progress: %d/%d repositories processed", i+1, len(repositories))
		i++
	}

	return // TODO remove after done testing update caching of tags

	// Early exit if we're just trying to get updated digests for unsupported manifests
	if *getUnsupportedManifests {
		return
	}

	// Create directories for
	manifestDir := fmt.Sprintf("%smanifest/", *saveDir)
	if err := os.MkdirAll(manifestDir, 0755); err != nil {
		log.Fatalf("Failed to create directory: %v", err)
	}
	configDir := fmt.Sprintf("%sconfig/", *saveDir)
	if err := os.MkdirAll(configDir, 0755); err != nil {
		log.Fatalf("Failed to create directory: %v", err)
	}

	// Open a file for writing a list of things that fail to fetch
	failedFetchesFile, err := os.OpenFile(fmt.Sprintf("%sfailed_fetches.txt", *saveDir), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("Failed to open failed fetches file: %v", err)
	}
	defer failedFetchesFile.Close()

	// Open a file for a list of the tags causing MANIFEST_SCHEMA_UNSUPPORTED errors
	unsupportedManifestSchemaFile, err := os.OpenFile(fmt.Sprintf("%sunsupported_manifest.txt", *saveDir), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("Failed to open unsupported manifest schema file: %v", err)
	}
	defer unsupportedManifestSchemaFile.Close()

	// Open a file for writing a list of failures to get rootfs layers
	badConfigsFile, err := os.OpenFile(fmt.Sprintf("%sbad_configs.txt", *saveDir), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("Failed to open failed fetches file: %v", err)
	}
	defer badConfigsFile.Close()

	i = 0
	for repo_name, tags := range tagData {
		for tagIdx, tag := range tags {
			if images, ok := tag["images"].([]interface{}); ok {
				tag_name, tagNameOk := tag["name"].(string)
				tagMediaType, _ := tag["media_type"].(string)
				if tagNameOk {
					tag_name = fmt.Sprintf(":%s", tag_name)
				} else {
					tag_name = ""
				}

				if tagMediaType == "application/vnd.docker.container.image.v1+json" || tagMediaType == "application/octet-stream" {
					if len(images) > 1 {
						if _, err := failedFetchesFile.WriteString(fmt.Sprintf("MULTIPLE IMAGES FOR TAG %s%s WITH media_type=application/vnd.docker.container.image.v1+json\n", repo_name, tag_name)); err != nil {
							log.Printf("Failed to write to failed fetches file: %v", err)
						}
						continue
					} else if tag_name == "" {
						if _, err := failedFetchesFile.WriteString(fmt.Sprintf("NO TAG NAME FOR TAG %s%s WITH media_type=application/vnd.docker.container.image.v1+json\n", repo_name, tag_name)); err != nil {
							log.Printf("Failed to write to failed fetches file: %v", err)
						}
						continue
					}
				}

				for _, image := range images {
					imageMap, _ := image.(map[string]interface{})
					architecture, archOk := imageMap["architecture"].(string)
					imgOS, osOk := imageMap["os"].(string)
					digest, digestOk := imageMap["digest"].(string)
					fmt.Printf("tagMediaType: %s\n", tagMediaType)
					if digestOk && archOk && osOk && architecture != "unknown" && imgOS != "unknown" && imgOS != "" {
						// Fetching the image manifest/config uses the digest to avoid dealing with tags and os/arch/variants
						configData, err := loadOrFetchConfig(puller, manifestDir, configDir, repo_name, tag_name, digest, tagMediaType)
						if err != nil {
							if strings.Contains(err.Error(), "unsupported MediaType: \"application/vnd.docker.distribution.manifest.v1+prettyjws\"") {
								// Fetch and save the config for the specified digest
								manifestv1, err := fetchRawManifest(puller, repo_name, "@"+digest)
								if err != nil {
									log.Printf("Failed to fetch raw manifest for %s%s@%s: %v", repo_name, tag_name, digest, err)
									if _, err := failedFetchesFile.WriteString(fmt.Sprintf("%s%s@%s: %v\n", repo_name, tag_name, digest, err)); err != nil {
										log.Printf("Failed to write to failed fetches file: %v", err)
									}
									continue
								}
								manifestSaveFile := fmt.Sprintf("%s%s.json", manifestDir, digest)
								if err := os.WriteFile(manifestSaveFile, manifestv1, 0644); err != nil {
									panic(err)
								}
								var manifestv1Data map[string]interface{}
								if err := json.Unmarshal(manifestv1, &manifestv1Data); err != nil {
									log.Printf("Failed to unmarshal manifestv1 data: %v", err)
									if _, err := failedFetchesFile.WriteString(fmt.Sprintf("%s%s@%s: Failed to unmarshal manifestv1 data: %v\n", repo_name, tag_name, digest, err)); err != nil {
										log.Printf("Failed to write to failed fetches file: %v", err)
									}
									continue
								}

								if schemaVersion, ok := manifestv1Data["schemaVersion"].(float64); !ok || schemaVersion != 1 {
									if err := os.WriteFile(manifestSaveFile, manifestv1, 0644); err != nil {
										log.Printf("Failed to write manifestv1 to file: %v", err)
										if _, err := failedFetchesFile.WriteString(fmt.Sprintf("%s%s@%s: Failed to write manifestv1 to file: %v\n", repo_name, tag_name, digest, err)); err != nil {
											log.Printf("Failed to write to failed fetches file: %v", err)
										}
										continue
									}
									if _, err := failedFetchesFile.WriteString(fmt.Sprintf("%s%s@%s: Manifest v1 schemaVersion is not 1\n", repo_name, tag_name, digest)); err != nil {
										log.Printf("Failed to write to failed fetches file: %v", err)
									}
									continue
								}
							}
							if strings.Contains(err.Error(), "MANIFEST_SCHEMA_UNSUPPORTED") {
								if _, err := unsupportedManifestSchemaFile.WriteString(fmt.Sprintf("%s%s@%s\n", repo_name, tag_name, digest)); err != nil {
									log.Printf("Failed to write to unsupported manifest schema file: %v", err)
								}
							}
							log.Printf("Failed to load or fetch config data for %s%s@%s: %v", repo_name, tag_name, digest, err)
							if _, err := failedFetchesFile.WriteString(fmt.Sprintf("%s%s@%s: %v\n", repo_name, tag_name, digest, err)); err != nil {
								log.Printf("Failed to write to failed fetches file: %v", err)
							}
							continue
						}
						var configJSON map[string]interface{}
						if err := json.Unmarshal(configData, &configJSON); err != nil {
							log.Printf("Failed to unmarshal config data: %v", err)
							if _, err := badConfigsFile.WriteString(fmt.Sprintf("%s%s@%s: Failed to unmarshal config data: %v\n", repo_name, tag_name, digest, err)); err != nil {
								log.Printf("Failed to write to bad configs file: %v", err)
							}
							continue
						}

						rootfs, ok := configJSON["rootfs"].(map[string]interface{})
						if !ok {
							log.Printf("Failed to get rootfs from config data for %s%s@%s", repo_name, tag_name, digest)
							if _, err := badConfigsFile.WriteString(fmt.Sprintf("%s%s@%s: Failed to get rootfs from config data: %v\n", repo_name, tag_name, digest, err)); err != nil {
								log.Printf("Failed to write to bad configs file: %v", err)
							}
							continue
						}

						/*diffIDs*/
						_, ok = rootfs["diff_ids"].([]interface{})
						if !ok {
							log.Printf("Failed to get diff_ids from rootfs for %s%s@%s", repo_name, tag_name, digest)
							if _, err := badConfigsFile.WriteString(fmt.Sprintf("%s%s@%s: Failed to get diff_ids from rootfs: %v\n", repo_name, tag_name, digest, err)); err != nil {
								log.Printf("Failed to write to bad configs file: %v", err)
							}
							continue
						}

						// diffIDStrings := make([]string, len(diffIDs))
						// for i, id := range diffIDs {
						// 	diffIDStrings[i], _ = id.(string)
						// }

						// variant, variantOk := imageMap["variant"].(string)
						// if variantOk && variant != "" {
						// 	fmt.Printf("Repo/Tag: %s%s, OS: %s, Architecture: %s, Variant: %s, Diff IDs: %v\n", repo_name, tag_name, imgOS, architecture, variant, diffIDStrings)
						// } else {
						// 	fmt.Printf("Repo/Tag: %s%s, OS: %s, Architecture: %s, Diff IDs: %v\n", repo_name, tag_name, imgOS, architecture, diffIDStrings)
						// }
					}
				}
			}
			log.Printf("Progress: %d/%d repositories processed, %d/%d tags processed", i, len(tagData), tagIdx, len(tags))
		}
		i++
	}

	// TODO after pulling everything:
	// - check what tags have an architecture but no os (or vice versa) and see what's up, if they need pulling
	// - check that all referenced manifests have been pulled, and that all config files referenced in manifests have been pulled
	// - construct json output file with repo:tag key that lists platforms and diff_ids and compressed layer hashes for each
	// - construct json output file keyed with digests from uncompressed layers that maps to repo:tag and platform info
	// - construct json output file with compressed layers that maps to repo:tag and platform info
	// - create sqlite database with the information from the above tables, indexed for fast look ups by repo/tag and digest
	// - see how many digests are shared between different tags/repos (e.g. one image uses another as a base layer?)
	// - figure out query procedure for determining the "right" base image repo:tag for a given digest
}
