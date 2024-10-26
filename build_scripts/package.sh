SOURCE_DIR="smartscore"
OUTPUT_DIR="output"
REDEPLOY=false
LAMBDA_ZIP="Code.zip"
S3_KEY="Code.zip"
BUCKET_STACK_NAME="codeBucket"
BUCKET_TEMPLATE_FILE="D:\code\smartScore\bucket_template.yaml"

LAMBDA_STACK_NAME="smartScore"
LAMBDA_TEMPLATE_FILE="D:\code\smartScore\lambda_template.yaml"
GET_TEAMS_NAME="GetTeams"
GET_PLAYERS_FROM_TEAM_NAME="GetPlayersFromTeam"
MAKE_PREDICTION_NAME="MakePredictions"
GET_TIMS_NAME="GetTims"
PERFORM_BACKFILL_NAME="PerformBackfilling"
PUBLISH_DB_NAME="PublishDb"
CHECK_COMPLETION_NAME="CheckCompleted"

OUTPUT_MOD_TIME=$(find "$OUTPUT_DIR" -type f -exec stat -c %Y {} + | sort -n | tail -1)

dependencies_needs_update() {
  # check if output is empty
  if [ -z "$(find "$OUTPUT_DIR" -type f)" ]; then
    return 0
  fi

  POETRY_MOD_TIME=$(find "poetry.lock" -type f -exec stat -c %Y {} + | sort -n | tail -1)

  if [ "$POETRY_MOD_TIME" -gt "$OUTPUT_MOD_TIME" ]; then
      return 0  # Update needed
  else
      return 1  # No update needed
  fi
}

code_needs_update() {
  # check if output doesn't contain event_handler
  if [ -z "$(find "$OUTPUT_DIR/event_handler.py" -type f)" ]; then
    return 0
  fi

  SOURCE_MOD_TIME=$(find "$SOURCE_DIR" -type f -exec stat -c %Y {} + | sort -n | tail -1)

  if [ "$SOURCE_MOD_TIME" -gt "$OUTPUT_MOD_TIME" ]; then
      return 0  # Update needed
  else
      return 1  # No update needed
  fi
}

c_files_needs_compilation() {
  return 0 # not sure if it is possible to check if .so is already compiled with linux vs windows, so forcing recompilation
}

bucket_yaml_needs_update() {
  # Check modification time of the bucket template file
  BUCKET_TEMPLATE_MOD_TIME=$(stat -c %Y "$BUCKET_TEMPLATE_FILE")

  # If the output directory is empty, redeployment is needed
  if [ -z "$(find "$OUTPUT_DIR" -type f)" ]; then
    return 0
  fi

  if [ "$BUCKET_TEMPLATE_MOD_TIME" -gt "$OUTPUT_MOD_TIME" ]; then
    return 0  # Bucket YAML file updated, redeployment needed
  else
    return 1  # No update needed
  fi
}

lambda_yaml_needs_update() {
  # Check modification time of the lambda template file
  LAMBDA_TEMPLATE_MOD_TIME=$(stat -c %Y "$LAMBDA_TEMPLATE_FILE")

  # If the output directory is empty, redeployment is needed
  if [ -z "$(find "$OUTPUT_DIR" -type f)" ]; then
    return 0
  fi

  if [ "$LAMBDA_TEMPLATE_MOD_TIME" -gt "$OUTPUT_MOD_TIME" ]; then
    return 0  # Lambda YAML file updated, redeployment needed
  else
    return 1  # No update needed
  fi
}

generate_zip_file() {
  echo "Creating ZIP package for Lambda..."
  cd $OUTPUT_DIR
  zip -r $LAMBDA_ZIP . > /dev/null
  cd ..

  ZIP_FILE_SIZE=$(stat -c%s "$OUTPUT_DIR/$LAMBDA_ZIP")
  ZIP_FILE_SIZE_MB=$((ZIP_FILE_SIZE / 1024 / 1024))

  echo "Size of ZIP file: $ZIP_FILE_SIZE_MB MB"

  if [ $ZIP_FILE_SIZE_MB -gt 25 ]; then
      echo "Error: The ZIP file exceeds 20 MB. Aborting deployment."
      exit 1
  fi
}

generate_bucket_stack() {
  echo "Creating CloudFormation stack for S3 bucket..." >&2
  aws cloudformation create-stack \
    --stack-name $BUCKET_STACK_NAME \
    --template-body file://$BUCKET_TEMPLATE_FILE \
    --capabilities CAPABILITY_NAMED_IAM

  echo "Waiting for S3 bucket stack creation to complete..." >&2
  aws cloudformation wait stack-create-complete --stack-name $BUCKET_STACK_NAME
  echo "S3 bucket stack creation completed." >&2

  S3_BUCKET_NAME=$(aws cloudformation describe-stacks \
    --stack-name $BUCKET_STACK_NAME \
    --query "Stacks[0].Outputs[?OutputKey=='CodeBucketName'].OutputValue" \
    --output text)

  if [ -z "$S3_BUCKET_NAME" ]; then
    echo "Error: Unable to retrieve the S3 bucket name." >&2
    return 1
  else
    echo "$S3_BUCKET_NAME"  # Only print the bucket name to stdout
    return 0
  fi
}

generate_lambda_stack() {
    aws cloudformation create-stack \
      --stack-name $LAMBDA_STACK_NAME \
      --template-body file://$LAMBDA_TEMPLATE_FILE \
      --capabilities CAPABILITY_NAMED_IAM

    # 5. Wait for the Lambda stack update to complete
  echo "Waiting for Lambda stack update to complete..."
  aws cloudformation wait stack-create-complete --stack-name $LAMBDA_STACK_NAME
}

update_lambda_functions() {
  local S3_BUCKET_NAME=$1
  aws lambda update-function-code --function-name "$GET_TEAMS_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$GET_PLAYERS_FROM_TEAM_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$MAKE_PREDICTION_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$GET_TIMS_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$PERFORM_BACKFILL_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$PUBLISH_DB_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1

  aws lambda update-function-code --function-name "$CHECK_COMPLETION_NAME" \
    --s3-bucket "$S3_BUCKET_NAME" --s3-key "$S3_KEY" > /dev/null 2>&1
}

mkdir -p $OUTPUT_DIR

if dependencies_needs_update; then
  REDEPLOY=true

  echo "Dependency changes detected. Updating the deployment package..."
  poetry export -f requirements.txt --output $OUTPUT_DIR/requirements.txt --without-hashes
  poetry run pip install --no-deps -r $OUTPUT_DIR/requirements.txt -t $OUTPUT_DIR
  rm -f $OUTPUT_DIR/requirements.txt
fi

if c_files_needs_compilation; then
  REDEPLOY=true

  echo "C Code changes detected. Updating the deployment package..."
  sh build_scripts/compile.sh
fi

if code_needs_update; then
  REDEPLOY=true

  echo "Code changes detected. Updating the deployment package..."
  cp -r $SOURCE_DIR/* $OUTPUT_DIR/
fi

if [ "$REDEPLOY" = true ]; then
  generate_zip_file

  S3_BUCKET_NAME=$(generate_bucket_stack 2>/dev/null)

  if [ $? -ne 0 ]; then
    echo "Failed to create or retrieve S3 bucket name." >&2
    exit 1
  else
    echo "S3 bucket created: $S3_BUCKET_NAME"
  fi

  echo "Uploading Lambda function to S3..."
  aws s3 cp $OUTPUT_DIR/$LAMBDA_ZIP s3://$S3_BUCKET_NAME/$S3_KEY

  echo "Making changes to the Lambda functions..."
  generate_lambda_stack
  update_lambda_functions "$S3_BUCKET_NAME"
fi