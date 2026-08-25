#!/bin/sh
set -eu
: "${DOCKERHUB_USERNAME:?Set DOCKERHUB_USERNAME}"
: "${DOCKERHUB_TOKEN:?Set DOCKERHUB_TOKEN}"
echo "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
docker build -t "$DOCKERHUB_USERNAME/intelboard:latest" .
docker push "$DOCKERHUB_USERNAME/intelboard:latest"
echo "Pushed: $DOCKERHUB_USERNAME/intelboard:latest"
