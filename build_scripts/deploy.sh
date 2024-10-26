#!/bin/bash

# Configuration variables
STACK_NAME="smartScoreBucket"
LAMBDA_STACK_NAME="smartScoreFunction"
LAMBDA_FUNCTION_NAME="GetAllPlayers"
LAMBDA_ZIP="Code.zip"
TEMPLATE_FILE="D:\code\smartScore\bucket_template.yaml"  # Updated to a new template for the bucket
LAMBDA_TEMPLATE_FILE="D:\code\smartScore\lambda_template.yaml"  # New template for Lambda function
S3_KEY="Code.zip"
SOURCE_DIR="smartscore"
OUTPUT_DIR="output"


#
## Create a new directory for your deployment package
#mkdir -p $OUTPUT_DIR
#
#
#
## DO SOMETHING LIKE THIS WITH POETRY.LOCK
#
## Function to check if the output directory needs an update
#needs_update() {
#    # Check if the output directory exists and is not empty
#    if [ -d "$OUTPUT_DIR" ] && [ "$(ls -A $OUTPUT_DIR)" ]; then
#        # Get the last modification time of the source directory and the output directory
#        SOURCE_MOD_TIME=$(find "$SOURCE_DIR" -type f -exec stat -c %Y {} + | sort -n | tail -1)
#        OUTPUT_MOD_TIME=$(find "$OUTPUT_DIR" -type f -exec stat -c %Y {} + | sort -n | tail -1)
#
#        # Compare modification times
#        if [ "$SOURCE_MOD_TIME" -gt "$OUTPUT_MOD_TIME" ]; then
#            return 0  # Update needed
#        else
#            return 1  # No update needed
#        fi
#    else
#        return 0  # Update needed if output directory does not exist or is empty
#    fi
#}
#
## Check if an update is needed
#if needs_update; then
#    echo "Changes detected. Updating the deployment package..."
#
#    # Export dependencies to a requirements file without hashes
#    echo "Exporting dependencies..."
#    poetry export -f requirements.txt --output $OUTPUT_DIR/requirements.txt --without-hashes
#
#    # Install dependencies into the output directory using pip within Poetry's environment
#    echo "Installing dependencies into output directory..."
#    poetry run pip install --no-deps -r $OUTPUT_DIR/requirements.txt -t $OUTPUT_DIR
#
#    # Copy your Lambda code into the output directory
#    cp -r $SOURCE_DIR/* $OUTPUT_DIR/
#
#    # Change to the output directory and create a zip file
#    echo "Creating ZIP package for Lambda..."
#    cd $OUTPUT_DIR
#    zip -r $LAMBDA_ZIP .  # Zip everything in the output directory
#    cd ..
#
#    # Check the size of the ZIP file
#    ZIP_FILE_SIZE=$(stat -c%s "$OUTPUT_DIR/$LAMBDA_ZIP")  # Get size in bytes
#    ZIP_FILE_SIZE_MB=$((ZIP_FILE_SIZE / 1024 / 1024))  # Convert to MB
#
#    echo "Size of ZIP file: $ZIP_FILE_SIZE_MB MB"
#
#    # Check if the ZIP file size exceeds 20 MB
#    if [ $ZIP_FILE_SIZE_MB -gt 20 ]; then
#        echo "Error: The ZIP file exceeds 20 MB. Aborting deployment."
#        exit 1
#    fi
#else
#    echo "No changes detected. Skipping deployment."
#    exit 0
#fi
#
#
#
#exit 0


## Create a new directory for your deployment package
#mkdir -p output
#
## Export dependencies to a requirements file
#poetry export -f requirements.txt --output requirements.txt
#
## Install dependencies in the output directory
#poetry run pip install --no-deps -r requirements.txt -t output/  # Install into the output directory
#
## Copy your Lambda code into the output directory
cp -r smartscore/* output/
#
# Change to the output directory and create a zip file
cd output
zip -r Code.zip .  # Zip everything in the output directory
cd ..


# Check the size of the ZIP file
ZIP_FILE_SIZE=$(stat -c%s "$OUTPUT_DIR/$LAMBDA_ZIP")  # Get size in bytes
ZIP_FILE_SIZE_MB=$((ZIP_FILE_SIZE / 1024 / 1024))  # Convert to MB

echo "Size of ZIP file: $ZIP_FILE_SIZE_MB MB"

# Check if the ZIP file size exceeds 20 MB
if [ $ZIP_FILE_SIZE_MB -gt 20 ]; then
    echo "Error: The ZIP file exceeds 20 MB. Aborting deployment."
    exit 1
fi

#
# 2. Create the S3 bucket stack
#echo "Creating CloudFormation stack for S3 bucket..."
#aws cloudformation create-stack \
#  --stack-name $STACK_NAME \
#  --template-body file://$TEMPLATE_FILE \
#  --capabilities CAPABILITY_NAMED_IAM
#
#echo "Waiting for S3 bucket stack creation to complete..."
#aws cloudformation wait stack-create-complete --stack-name $STACK_NAME
#echo "S3 bucket stack creation completed."
#
# Retrieve the S3 bucket name from the CloudFormation stack's output
S3_BUCKET_NAME=$(aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --query "Stacks[0].Outputs[?OutputKey=='CodeBucketName'].OutputValue" \
  --output text)

if [ -z "$S3_BUCKET_NAME" ]; then
  echo "Error: Unable to retrieve the S3 bucket name."
  exit 1
else
  echo "S3 Bucket Name: $S3_BUCKET_NAME"
fi

# 3. Upload the Lambda zip to S3 after confirming the bucket stack exists
echo "Uploading Lambda function to S3..."
aws s3 cp $OUTPUT_DIR/$LAMBDA_ZIP s3://$S3_BUCKET_NAME/$S3_KEY

# 4. Create or update the Lambda function stack
echo "Creating or updating CloudFormation stack for Lambda function..."
STACK_STATUS=$(aws cloudformation describe-stacks --stack-name $LAMBDA_STACK_NAME 2>&1)

if [[ $STACK_STATUS == *"does not exist"* ]]; then
    echo "Stack does not exist. Creating a new CloudFormation stack for Lambda function..."

    aws cloudformation create-stack \
      --stack-name $LAMBDA_STACK_NAME \
      --template-body file://$LAMBDA_TEMPLATE_FILE \
      --capabilities CAPABILITY_NAMED_IAM

    # 5. Wait for the Lambda stack update to complete
  echo "Waiting for Lambda stack update to complete..."
  aws cloudformation wait stack-create-complete --stack-name $LAMBDA_STACK_NAME

else
    echo "Stack exists. Updating the CloudFormation stack for Lambda function..."

    aws cloudformation update-stack \
      --stack-name $LAMBDA_STACK_NAME \
      --template-body file://$LAMBDA_TEMPLATE_FILE \
      --capabilities CAPABILITY_NAMED_IAM

    # 5. Wait for the Lambda stack update to complete
  echo "Waiting for Lambda stack update to complete..."
  aws cloudformation wait stack-update-complete --stack-name $LAMBDA_STACK_NAME

fi

echo "Lambda stack update completed."

# 6. Confirm deployment success
echo "Deployment successful. Lambda function '$LAMBDA_FUNCTION_NAME' is up to date."
