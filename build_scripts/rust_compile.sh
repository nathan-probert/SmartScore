#!/bin/bash

# Define the absolute path to the project in windows format
PROJECT_PATH="C:\Users\natha\Documents\Code\SmartScore\SmartScore"

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
    ls

    cd /smartscore/Rust/make_predictions

    cargo build --release --target x86_64-unknown-linux-gnu
fi

# Print a message upon successful completion
echo "Compilation completed. Check the target/release directory in $PROJECT_PATH/smartscore/"
exit 1
