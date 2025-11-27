#!/bin/bash

set -e

# Function to prompt for a version number
get_version() {
    read -p "Please provide a version number: " version
    if [[ -z "$version" ]]; then
        echo "Version number is required. Exiting."
        exit 1
    fi
}

# Function to tag and push images
tag_and_push() {
    local image_name="$1"
    local version="$2"

    # Change this prefix if you want a different Docker Hub namespace/repo pattern
    local repo="italiandogs/spambanner-${image_name}"

    echo "Tagging local image '${image_name}' as:"
    echo "  ${repo}:${version}"
    echo "  ${repo}:latest"

    docker tag "${image_name}" "${repo}:${version}"
    docker tag "${image_name}" "${repo}:latest"

    echo "Pushing ${repo}:${version} and ${repo}:latest ..."
    docker push "${repo}:${version}"
    docker push "${repo}:latest"

    echo "Done pushing ${repo}."
}

# Function to optionally build Docker images
build_docker_images() {
    echo "Would you like to build the Docker image before tagging and pushing?"
    echo "1. Yes"
    echo "2. No"
    read -p "Enter your choice (1-2): " build_choice

    case "$build_choice" in
        1)
            echo "Building Docker image via docker-compose..."
            docker compose -f ./docker-compose.yml build spam-banner-bot
            echo "Build completed."
            ;;
        2)
            echo "Skipping build."
            ;;
        *)
            echo "Invalid choice. Skipping build."
            ;;
    esac
}

# Start script
echo "Select an option to tag and upload:"
echo "1. Spam Banner Bot"
echo "0. Exit"
read -p "Enter your choice (0-1): " choice

# Collect version number and build option
get_version
build_docker_images

# Perform action based on choice
case "$choice" in
    1)
        tag_and_push "spam-banner-bot" "$version"
        ;;
    0)
        echo "Exiting script."
        exit 0
        ;;
    *)
        echo "Invalid choice. Exiting script."
        exit 1
        ;;
esac
