#!/bin/bash

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_PATH=$(dirname "$SCRIPT_DIR")
PROJECT_PATH="$(cygpath.exe -C ANSI -w -p "${PROJECT_PATH}")"

# Run the Docker container with volume mounting
if ! docker run -it --rm -v "$PROJECT_PATH:/project" amazonlinux:2 sh -c "
    # Install required tools
    yum update -y
    yum install -y gcc gcc-c++ make

    # Navigate to the C source directory
    cd /project

    # Compile the C code into a shared object
    make compile
"; then
    echo "Running for linux environment."
    echo "If you are on windows, ensure docker is running."

    make compile
fi

# Print a message upon successful completion
echo "Compilation completed. Check the compiled_code.so in $PROJECT_PATH/smartscore/"
