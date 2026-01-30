import requests
import time
import logging
import argparse
import os
import json

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def fetch_data(url, retries=5, backoff_factor=0.3):
    """Fetch data from the given URL with retries and exponential backoff."""
    for i in range(retries):
        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logging.warning(f"Request failed: {e}. Retrying in {backoff_factor * (2 ** i)} seconds...")
            time.sleep(backoff_factor * (2 ** i))
    logging.error(f"Failed to fetch data from {url} after {retries} retries.")
    return None

def fetch_all_pages(url):
    """Fetch all pages of data from a paginated API endpoint."""
    results = []
    while url:
        data = fetch_data(url)
        if data:
            results.extend(data['results'])
            url = data.get('next')
        else:
            break
    return results

def save_json(data, file_path):
    """Save JSON data to a file."""
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

def main(save_json_dir=None):
    base_url = "https://hub.docker.com/v2/repositories/library/"
    repositories = fetch_all_pages(f"{base_url}?page_size=100")

    # TODO: since we scraped all this information already, add a mode that avoids query and uses downloaded data to regenerate mapping json (should de-duplicate SHA hash keys, and list out os/arch platform tag for the image when regenerating it to reduce number of authenticated queries required to get config files)
    if save_json_dir:
        os.makedirs(save_json_dir, exist_ok=True)
        save_json(repositories, os.path.join(save_json_dir, "repositories.json"))

    image_mapping = {}

    for repo in repositories:
        if 'name' not in repo:
            logging.warning(f"No name in repo: {repo}")
        repo_name = repo['name']
        logging.info(f"Fetching tags for repository: {repo_name}")
        tags = fetch_all_pages(f"{base_url}{repo_name}/tags?page_size=100")

        if save_json_dir:
            save_json(tags, os.path.join(save_json_dir, f"{repo_name}.json"))

        for tag in tags:
            if 'name' not in tag:
                logging.warning(f"No tag name for tag in {repo_name}: {tag}")
            tag_name = tag['name']
            if 'images' not in tag:
                logging.warning(f"No images for {repo_name}:{tag_name}: {tag}")
            for image in tag['images']:
                if 'digest' in image:
                    digest = image['digest']
                    image_mapping[digest] = f"{repo_name}:{tag_name}"
                else:
                    logging.warning(f"No digest in image for {repo_name}:{tag_name}: {image}")

    if save_json_dir:
        save_json(image_mapping, os.path.join(save_json_dir, "image_mapping.json"))

    return image_mapping

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Docker image ID to official image mapping.")
    parser.add_argument("--save-json", type=str, help="Directory to save JSON responses.")
    args = parser.parse_args()

    main(save_json_dir=args.save_json)
