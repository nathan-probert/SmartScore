#!/bin/bash

# Compiles SmartScore's C and Rust extensions inside a single manylinux
# container (see build_scripts/Dockerfile).
#
# In CI the compiler image is built and cached with Docker BuildKit before
# this script runs; if it is missing (e.g. first local use), it is built here.
#
# Outputs (written into the mounted workspace, matching the layout the rest of
# the build/deploy expects):
#   smartscore/compiled_code.so
#   smartscore/Rust/make_predictions/target/x86_64-unknown-linux-gnu/release/libmake_predictions_rust.so

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_PATH=$(dirname "$SCRIPT_DIR")

# Convert to Windows path only when cygpath is available (Git Bash on Windows).
if command -v cygpath.exe >/dev/null 2>&1; then
    PROJECT_PATH="$(cygpath.exe -C ANSI -w -p "${PROJECT_PATH}")"
fi

IMAGE=${BUILD_IMAGE:-smartscore-build-env:latest}

# Build the compiler image on first use (local machines).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building compiler environment image $IMAGE..."
    docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$PROJECT_PATH"
fi

echo "Compiling C and Rust code in $IMAGE..."
docker run --rm -v "$PROJECT_PATH:/project" "$IMAGE" sh -c "
    cd /project
    make compile_c
    cd /project/smartscore/Rust/make_predictions
    cargo build --release --target x86_64-unknown-linux-gnu
"

echo "Compilation completed. Check the artifacts in $PROJECT_PATH/smartscore/"