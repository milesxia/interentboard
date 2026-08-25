#!/bin/sh
set -eu

IMAGE="${INTERNETBOARD_IMAGE:-milesxia/internetboard:latest}"
USER="${DOCKERHUB_USERNAME:-milesxia}"

echo "[1/4] Install test dependencies"
python -m pip install -r requirements-dev.txt >/dev/null

echo "[2/4] Validate source"
PYTHONPATH=. MOCK_AI=true AUTO_PULL_MODEL=false pytest -q
PYTHONPATH=. MOCK_AI=true AUTO_PULL_MODEL=false python scripts/smoke_test.py

echo "[3/4] Docker Hub login"
printf "Docker Hub Token: "
stty -echo
read TOKEN
stty echo
printf "\n"
printf "%s" "$TOKEN" | docker login -u "$USER" --password-stdin
unset TOKEN

echo "[4/4] Build and push $IMAGE"
docker buildx inspect internetboard-builder >/dev/null 2>&1 || docker buildx create --name internetboard-builder --use
docker buildx use internetboard-builder
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
printf "Pushed: %s\n" "$IMAGE"
