#!/bin/bash

# Define the path to your project
PROJECT_PATH="D:\code\smartScore"

# Run the Docker container with volume mounting
docker run -it --rm -v "$PROJECT_PATH:/project" amazonlinux:2 sh -c "
    # Install required tools
    yum update -y
    yum install -y gcc gcc-c++ make

    # Navigate to the C source directory
    cd /project

    # Compile the C code into a shared object
    make compile
"

# Print a message upon completion
echo "Compilation completed. Check the compiled_code.so in $PROJECT_PATH/smartscore/"
