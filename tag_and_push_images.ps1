param()

function Get-Version {
    param([ref]$Version)

    $v = Read-Host "Please provide a version number"
    if (-not $v) {
        Write-Host "Version number is required. Exiting."
        exit 1
    }
    $Version.Value = $v
}

function Tag-AndPush {
    param(
        [string]$ImageName,
        [string]$Version
    )

    # Adjust this prefix if you want a different Docker Hub namespace/repo pattern
    $repo = "italiandogs/spambanner-$ImageName"

    Write-Host "Tagging local image '$ImageName' as:"
    Write-Host "  ${repo}:${Version}"
    Write-Host "  ${repo}:latest"

    docker tag $ImageName "${repo}:${Version}"
    docker tag $ImageName "${repo}:latest"

    Write-Host "Pushing ${repo}:${Version} and ${repo}:latest ..."
    docker push "${repo}:${Version}"
    docker push "${repo}:latest"

    Write-Host "Done pushing ${repo}."
}

function Build-DockerImages {
    Write-Host "Would you like to build the Docker image before tagging and pushing?"
    Write-Host "1. Yes"
    Write-Host "2. No"
    $choice = Read-Host "Enter your choice (1-2)"

    switch ($choice) {
        "1" {
            Write-Host "Building Docker image via docker-compose..."
            docker compose -f ./docker-compose.yml build spam-banner-bot
            Write-Host "Build completed."
        }
        "2" {
            Write-Host "Skipping build."
        }
        default {
            Write-Host "Invalid choice. Skipping build."
        }
    }
}

Write-Host "Select an option to tag and upload:"
Write-Host "1. Spam Banner Bot"
Write-Host "0. Exit"
$choice = Read-Host "Enter your choice (0-1)"

# Get version and optionally build
$versionRef = ""
Get-Version ([ref]$versionRef)
Build-DockerImages

switch ($choice) {
    "1" {
        Tag-AndPush -ImageName "spam-banner-bot" -Version $versionRef
    }
    "0" {
        Write-Host "Exiting script."
        exit 0
    }
    default {
        Write-Host "Invalid choice. Exiting."
        exit 1
    }
}
