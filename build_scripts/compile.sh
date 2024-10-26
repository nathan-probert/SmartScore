#!/bin/bash

# Define the path to your project
PROJECT_PATH="./smartScore"

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
    echo "Error: Docker daemon is not running or another error occurred."
    exit 1  # Exit the script with a non-zero exit code
fi

# Print a message upon successful completion
echo "Compilation completed. Check the compiled_code.so in $PROJECT_PATH/smartscore/"
