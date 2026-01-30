package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	_ "github.com/mattn/go-sqlite3"
)

type ImageInfo struct {
	Repo         string `json:"repo"`
	Tag          string `json:"tag"`
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
	Variant      string `json:"variant,omitempty"`
}

func processData(saveDir string) {
	tagsDir := filepath.Join(saveDir, "tags")
	manifestDir := filepath.Join(saveDir, "manifest")
	configDir := filepath.Join(saveDir, "config")

	// Data structures for JSON output
	// repo:tag -> info
	repoTags := make(map[string]map[string]interface{})
	// uncompressed digest -> list of images containing it
	uncompressedLayerMap := make(map[string][]ImageInfo)
	// compressed digest -> list of images containing it
	compressedLayerMap := make(map[string][]ImageInfo)

	// Open SQLite DB
	dbPath := filepath.Join(saveDir, "docker_images.db")
	os.Remove(dbPath) // Start fresh
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}
	defer db.Close()

	createTables(db)

	files, err := os.ReadDir(tagsDir)
	if err != nil {
		log.Printf("Failed to read tags directory: %v", err)
		return
	}

	log.Println("Starting data processing and database generation...")

	for _, file := range files {
		if !strings.HasSuffix(file.Name(), ".json") {
			continue
		}
		repoName := strings.TrimSuffix(file.Name(), ".json")
		var tags []map[string]interface{}
		bytes, err := os.ReadFile(filepath.Join(tagsDir, file.Name()))
		if err != nil {
			log.Printf("Failed to read tag file %s: %v", file.Name(), err)
			continue
		}
		if err := json.Unmarshal(bytes, &tags); err != nil {
			log.Printf("Failed to unmarshal tag file %s: %v", file.Name(), err)
			continue
		}

		for _, tag := range tags {
			tagName, _ := tag["name"].(string)
			tagMediaType, _ := tag["media_type"].(string)
			images, ok := tag["images"].([]interface{})
			if !ok {
				continue
			}

			if tagName != "" {
				tagName = ":" + tagName
			}

			repoTagKey := fmt.Sprintf("%s%s", repoName, tagName)
			repoTags[repoTagKey] = make(map[string]interface{})
			var platforms []map[string]interface{}

			// Insert into DB
			tx, err := db.Begin()
			if err != nil {
				log.Printf("Failed to begin transaction: %v", err)
				continue
			}

			for _, image := range images {
				imgMap, _ := image.(map[string]interface{})
				digest, _ := imgMap["digest"].(string)
				arch, _ := imgMap["architecture"].(string)
				osVal, _ := imgMap["os"].(string)
				variant, _ := imgMap["variant"].(string)

				if digest == "" {
					continue
				}

				// Determine manifest path
				var manifestPath string
				if tagMediaType == "application/vnd.docker.container.image.v1+json" || tagMediaType == "application/octet-stream" {
					manifestPath = filepath.Join(manifestDir, fmt.Sprintf("%s.json", repoTagKey))
				} else {
					manifestPath = filepath.Join(manifestDir, digest+".json")
				}

				manifestBytes, err := os.ReadFile(manifestPath)
				if err != nil {
					// It's normal for some to be missing if we failed to fetch them or they are unsupported
					continue
				}

				var manifestData map[string]interface{}
				if err := json.Unmarshal(manifestBytes, &manifestData); err != nil {
					continue
				}

				configDigest, _ := manifestData["config"].(map[string]interface{})["digest"].(string)

				// Get compressed layers from manifest
				layers, _ := manifestData["layers"].([]interface{})
				var compressedLayers []string
				for _, layer := range layers {
					l, _ := layer.(map[string]interface{})
					if d, ok := l["digest"].(string); ok {
						compressedLayers = append(compressedLayers, d)
					}
				}

				// Load config
				configBytes, err := os.ReadFile(filepath.Join(configDir, configDigest+".json"))
				if err != nil {
					continue
				}

				var configData map[string]interface{}
				if err := json.Unmarshal(configBytes, &configData); err != nil {
					continue
				}

				rootfs, _ := configData["rootfs"].(map[string]interface{})
				diffIDs, _ := rootfs["diff_ids"].([]interface{})
				var uncompressedLayers []string
				for _, id := range diffIDs {
					if s, ok := id.(string); ok {
						uncompressedLayers = append(uncompressedLayers, s)
					}
				}

				info := ImageInfo{
					Repo:         repoName,
					Tag:          tagName,
					OS:           osVal,
					Architecture: arch,
					Variant:      variant,
				}

				// Populate maps
				for _, layer := range uncompressedLayers {
					uncompressedLayerMap[layer] = append(uncompressedLayerMap[layer], info)
				}
				for _, layer := range compressedLayers {
					compressedLayerMap[layer] = append(compressedLayerMap[layer], info)
				}

				// Platform info for repoTags
				platformInfo := map[string]interface{}{
					"os":                  osVal,
					"architecture":        arch,
					"variant":             variant,
					"digest":              digest,
					"config_digest":       configDigest,
					"compressed_layers":   compressedLayers,
					"uncompressed_layers": uncompressedLayers,
				}
				platforms = append(platforms, platformInfo)

				if err := insertImage(tx, repoName, tagName, digest, configDigest, arch, osVal, variant); err != nil {
					log.Printf("Error inserting image: %v", err)
				}
				if err := insertLayers(tx, digest, "compressed", compressedLayers); err != nil {
					log.Printf("Error inserting compressed layers: %v", err)
				}
				if err := insertLayers(tx, digest, "uncompressed", uncompressedLayers); err != nil {
					log.Printf("Error inserting uncompressed layers: %v", err)
				}
			}
			if err := tx.Commit(); err != nil {
				log.Printf("Failed to commit transaction: %v", err)
			}
			repoTags[repoTagKey]["platforms"] = platforms
		}
	}

	log.Println("Saving JSON mappings...")
	saveJSON(repoTags, filepath.Join(saveDir, "repo_tags_map.json"))
	saveJSON(uncompressedLayerMap, filepath.Join(saveDir, "uncompressed_layer_map.json"))
	saveJSON(compressedLayerMap, filepath.Join(saveDir, "compressed_layer_map.json"))
	log.Println("Processing complete.")
}

func createTables(db *sql.DB) {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS images (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			repo TEXT,
			tag TEXT,
			digest TEXT,
			config_digest TEXT,
			architecture TEXT,
			os TEXT,
			variant TEXT
		);`,
		`CREATE TABLE IF NOT EXISTS layers (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			image_digest TEXT,
			layer_type TEXT, -- 'compressed' or 'uncompressed'
			layer_digest TEXT,
			layer_index INTEGER
		);`,
		`CREATE INDEX IF NOT EXISTS idx_images_repo_tag ON images(repo, tag);`,
		`CREATE INDEX IF NOT EXISTS idx_layers_layer_digest ON layers(layer_digest);`,
		`CREATE INDEX IF NOT EXISTS idx_layers_image_digest ON layers(image_digest);`,
	}
	for _, q := range queries {
		_, err := db.Exec(q)
		if err != nil {
			log.Fatalf("Failed to create table: %v", err)
		}
	}
}

func insertImage(tx *sql.Tx, repo, tag, digest, configDigest, arch, osVal, variant string) error {
	_, err := tx.Exec(`INSERT INTO images (repo, tag, digest, config_digest, architecture, os, variant) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		repo, tag, digest, configDigest, arch, osVal, variant)
	if err != nil {
		return err
	}
	return nil
}

func insertLayers(tx *sql.Tx, imageDigest, layerType string, layers []string) error {
	for i, layer := range layers {
		_, err := tx.Exec(`INSERT INTO layers (image_digest, layer_type, layer_digest, layer_index) VALUES (?, ?, ?, ?)`,
			imageDigest, layerType, layer, i)
		if err != nil {
			return err
		}
	}
	return nil
}
