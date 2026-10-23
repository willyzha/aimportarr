#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

IMAGE_NAME="aimportarr:test"

echo "=================================================="
echo " Running Docker Container Smoke Tests"
echo "=================================================="

# Check if Docker is available locally or via remote SSH (when UNRAID_HOST is explicitly set)
if command -v docker &> /dev/null; then
    DOCKER_CMD="docker"
elif [ -n "$UNRAID_HOST" ]; then
    REMOTE_HOST="$UNRAID_HOST"
    echo "Local docker not found. Using remote docker host on $REMOTE_HOST..."

    # Sync repo to remote /tmp/aimportarr-test
    ssh "$REMOTE_HOST" "rm -rf /tmp/aimportarr-test && mkdir -p /tmp/aimportarr-test"
    tar --exclude='.git' -cf - . | ssh "$REMOTE_HOST" "tar -xf - -C /tmp/aimportarr-test"

    echo "Building test image on remote Docker..."
    ssh "$REMOTE_HOST" "cd /tmp/aimportarr-test && docker build -t $IMAGE_NAME ."

    echo "Running container test suite..."
    ssh "$REMOTE_HOST" "docker run --rm -v /tmp/aimportarr-test/config.example.yml:/app/config.example.yml:ro -v /tmp/aimportarr-test/tests:/app/tests:ro $IMAGE_NAME python -m unittest discover -s tests -v"

    echo "Cleaning up remote test files..."
    ssh "$REMOTE_HOST" "rm -rf /tmp/aimportarr-test && docker rmi $IMAGE_NAME >/dev/null 2>&1 || true"

    echo "=================================================="
    echo "✔ All Docker tests passed successfully on $REMOTE_HOST!"
    echo "=================================================="
    exit 0
else
    echo "ERROR: Docker is not installed locally (and UNRAID_HOST is not set)."
    exit 1
fi

echo "Building local Docker test image ($IMAGE_NAME)..."
$DOCKER_CMD build -t "$IMAGE_NAME" .

echo "Running container test suite..."
$DOCKER_CMD run --rm -v "$REPO_DIR/config.example.yml:/app/config.example.yml:ro" -v "$REPO_DIR/tests:/app/tests:ro" "$IMAGE_NAME" python -m unittest discover -s tests -v

echo "Cleaning up test image..."
$DOCKER_CMD rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

echo "=================================================="
echo "✔ All Docker container tests passed successfully!"
echo "=================================================="
