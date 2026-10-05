#!/usr/bin/env bash
# Operator-run only: build and publish an image, with no cluster/deployment action.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Use a clean checkout of the reviewed compatibility commit." >&2
  exit 1
fi
revision=$(git rev-parse HEAD)
region=us-east-1
account=438465164022
repository=mmrf/gen3-analysis
registry="$account.dkr.ecr.$region.amazonaws.com"
base_tag=f87a2abb3ce50893b7c665c29a6ef781c3054fa5
if [[ "$(aws sts get-caller-identity --query Account --output text)" != "$account" ]]; then
  echo "Select AWS credentials for the MMRF image registry account $account." >&2
  exit 1
fi
base_digest=$(aws ecr describe-images --region "$region" --registry-id "$account" \
  --repository-name "$repository" --image-ids "imageTag=$base_tag" \
  --query 'imageDetails[0].imageDigest' --output text)
if [[ ! "$base_digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "Could not resolve the existing deployed image digest." >&2
  exit 1
fi
aws ecr get-login-password --region "$region" |
  docker login --username AWS --password-stdin "$registry"

build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
mkdir "$build_dir/gen3analysis"
cp gen3analysis/auth.py gen3analysis/settings.py "$build_dir/gen3analysis/"
cp Dockerfile.fence-compat "$build_dir/Dockerfile"
image="$registry/$repository:$revision"
docker build --platform linux/amd64 \
  --build-arg "BASE_IMAGE=$registry/$repository@$base_digest" \
  --build-arg "SOURCE_REVISION=$revision" -t "$image" "$build_dir"
docker run --rm --network none --platform linux/amd64 \
  --entrypoint /gen3analysis/.venv/bin/python \
  -e ACCESS_TOKEN_AUDIENCE=gen3 "$image" \
  -c 'from gen3analysis.settings import settings; from gen3analysis.auth import Auth; assert settings.ACCESS_TOKEN_AUDIENCE == "gen3"; print("Analysis imports and configured audience verified")'
docker push "$image"
aws ecr describe-images --region "$region" --registry-id "$account" \
  --repository-name "$repository" --image-ids "imageTag=$revision" \
  --query 'imageDetails[0].{tags:imageTags,digest:imageDigest}'
echo "Published $image; no deployment was changed."
