#!/bin/bash

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_PATH=$(dirname "$SCRIPT_DIR")
PROJECT_PATH="$(cygpath.exe -C ANSI -w -p "${PROJECT_PATH}")"

# Run the Docker container with volume mounting
if ! docker run -it --rm -v "$PROJECT_PATH:/project" quay.io/pypa/manylinux_2_28_x86_64 sh -c "
    # Install required tools for Rust
    yum update -y
    yum groupinstall -y 'Development Tools'
    yum install -y rust-toolset

    # Navigate to the Rust project directory
    cd /project
    cd /smartscore/Rust/make_predictions

    # Compile the Rust code (assuming it's using Cargo)
    cargo build --release --target x86_64-unknown-linux-gnu
"; then
    echo "Running for linux environment."
    echo "If you are on windows, ensure docker is running."
    cd smartscore/Rust/make_predictions

    cargo build --release --target x86_64-unknown-linux-gnu
fi

# Print a message upon successful completion
echo "Compilation completed. Check the target/release directory in $PROJECT_PATH/smartscore/"
