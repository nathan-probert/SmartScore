#!/bin/bash

# one of {dev, prod}
ENV=${ENV:-dev}  # If ENV is not set, default to "dev"

AWS_REGION=${AWS_REGION:-us-east-1}

MAX_ZIP_SIZE_MB=25

SOURCE_DIR="smartscore"
OUTPUT_DIR="output"

STACK_NAME="SmartScore-$ENV"
TEMPLATE_FILE="./templates/template.yaml"

KEY="$STACK_NAME.zip"

LAMBDA_FUNCTIONS=(
  "GetTeams-$ENV"
  "GetPlayersFromTeam-$ENV"
  "MakePredictions-$ENV"
  "GetTims-$ENV"
  "PerformBackfilling-$ENV"
  "PublishDb-$ENV"
  "CheckCompleted-$ENV"
  "ParseData-$ENV"
  "UpdateHistory-$ENV"
)


generate_smartscore_stack() {
  if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_API_KEY" ]; then
    echo "Error: SUPABASE_URL or SUPABASE_API_KEY environment variables are not set."
    exit 1
  fi

  if aws cloudformation describe-stacks --stack-name "$STACK_NAME" &>/dev/null; then
    echo "Updating CloudFormation stack $STACK_NAME..."
    UPDATE_OUTPUT=$(aws cloudformation update-stack \
      --stack-name "$STACK_NAME" \
      --template-body file://"$TEMPLATE_FILE" \
      --parameters ParameterKey=ENV,ParameterValue="$ENV" \
        ParameterKey=SupabaseUrl,ParameterValue="$SUPABASE_URL" \
        ParameterKey=SupabaseApiKey,ParameterValue="$SUPABASE_API_KEY" \
      --capabilities CAPABILITY_NAMED_IAM 2>&1)

    if echo "$UPDATE_OUTPUT" | grep -q "No updates are to be performed."; then
      echo "No updates needed. Skipping wait."
    else
      echo "Waiting for CloudFormation stack update to complete..."
      aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"
    fi
  else
    echo "Creating CloudFormation stack $STACK_NAME..."
    aws cloudformation create-stack \
      --stack-name "$STACK_NAME" \
      --template-body file://"$TEMPLATE_FILE" \
      --parameters ParameterKey=ENV,ParameterValue="$ENV" \
        ParameterKey=SupabaseUrl,ParameterValue="$SUPABASE_URL" \
        ParameterKey=SupabaseApiKey,ParameterValue="$SUPABASE_API_KEY" \
      --capabilities CAPABILITY_NAMED_IAM

    echo "Waiting for CloudFormation stack creation to complete..."
    aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"
  fi

  # Check the final status of the stack
  STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].StackStatus" --output text)

  if [[ "$STACK_STATUS" != "CREATE_COMPLETE" && "$STACK_STATUS" != "UPDATE_COMPLETE" ]]; then
    echo "CloudFormation stack operation failed with status: $STACK_STATUS."
    exit 1  # Exit with error
  fi

  echo "CloudFormation stack $STACK_NAME completed successfully with status: $STACK_STATUS."
}


generate_zip_file() {
  echo "Creating ZIP package for Lambda..."
  cd $OUTPUT_DIR

  # Exclude any .zip files from the ZIP package
  zip -r $KEY . -x "*.zip" > /dev/null

  cd ..

  ZIP_FILE_SIZE=$(stat -c%s "$OUTPUT_DIR/$KEY")
  ZIP_FILE_SIZE_MB=$((ZIP_FILE_SIZE / 1024 / 1024))

  echo "Size of ZIP file: $ZIP_FILE_SIZE_MB MB"

  if [ $ZIP_FILE_SIZE_MB -gt $MAX_ZIP_SIZE_MB ]; then
      echo "Error: The ZIP file exceeds $MAX_ZIP_SIZE_MB MB. Aborting deployment."
      exit 1
  fi
}


update_lambda_code() {
  for FUNCTION in "${LAMBDA_FUNCTIONS[@]}"; do
    echo "Updating Lambda function code: $FUNCTION..."

    aws lambda update-function-code \
      --function-name "$FUNCTION" \
      --zip-file fileb://$OUTPUT_DIR/$KEY &>/dev/null  # Suppress all output

    if [ $? -ne 0 ]; then
      echo "Error: Failed to update Lambda function code: $FUNCTION."
      exit 1
    fi

    echo "Lambda function code updated successfully: $FUNCTION."
  done
}


deploy_state_machine() {
  local NAME=$1
  local DEFINITION_FILE=$2

  local STATE_MACHINE_NAME="$NAME-$ENV"
  local PATCHED_FILE="/tmp/${STATE_MACHINE_NAME}.json"

  # Ensure required env vars exist
  if [ -z "$AWS_REGION" ] || [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "Missing AWS_REGION or AWS_ACCOUNT_ID"
    exit 1
  fi

  # Patch the definition file
  envsubst < "$DEFINITION_FILE" > "$PATCHED_FILE"

  # Get the role ARN from CloudFormation outputs
  local ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='StepFunctionExecutionRoleArn'].OutputValue" \
    --output text)

  # Deploy the state machine
  if aws stepfunctions describe-state-machine \
      --state-machine-arn "arn:aws:states:$AWS_REGION:$AWS_ACCOUNT_ID:stateMachine:$STATE_MACHINE_NAME" &>/dev/null; then
    echo "Updating Step Function: $STATE_MACHINE_NAME..."
    aws stepfunctions update-state-machine \
      --state-machine-arn "arn:aws:states:$AWS_REGION:$AWS_ACCOUNT_ID:stateMachine:$STATE_MACHINE_NAME" \
      --definition file://"$PATCHED_FILE" \
      --role-arn "$ROLE_ARN"
  else
    echo "Creating Step Function: $STATE_MACHINE_NAME..."
    aws stepfunctions create-state-machine \
      --name "$STATE_MACHINE_NAME" \
      --definition file://"$PATCHED_FILE" \
      --role-arn "$ROLE_ARN" \
      --type STANDARD
  fi

  echo "Step Function '$STATE_MACHINE_NAME' deployed successfully."
}


# main

# create empty output directory
echo "Creating output directory..."
rm -rf $OUTPUT_DIR
mkdir -p $OUTPUT_DIR

# update dependencies
echo "Updating dependencies..."
poetry export -f requirements.txt --output $OUTPUT_DIR/requirements.txt --without-hashes
poetry run pip install --no-deps -r $OUTPUT_DIR/requirements.txt -t $OUTPUT_DIR
rm -f $OUTPUT_DIR/requirements.txt

# compile C code
echo "Compiling C code..."
sh build_scripts/compile.sh
if [ $? -ne 0 ]; then
  echo "Error: Compilation failed. Ensure docker is running."
  exit 1
fi

# compile Rust code
echo "Compiling Rust code..."
sh build_scripts/rust_compile.sh
if [ $? -ne 0 ]; then
  echo "Error: Compilation failed. Ensure docker is running."
  exit 1
fi

# update the code
echo "Updating the code..."
cp -r $SOURCE_DIR/* $OUTPUT_DIR
cp -r $OUTPUT_DIR/Rust/make_predictions/target/x86_64-unknown-linux-gnu/release/libmake_predictions_rust.so $OUTPUT_DIR/make_predictions_rust.so
rm -rf $OUTPUT_DIR/C
rm -rf $OUTPUT_DIR/Rust

# generate the ZIP file
generate_zip_file

# create the CloudFormation stack for smartscore
generate_smartscore_stack

# update the Lambda function code
update_lambda_code

# deploy Step Functions
echo "Deploying Step Functions..."
deploy_state_machine "GetAllPlayers" "templates/get_all_players.asl.json"
deploy_state_machine "GetPlayers" "templates/get_players.asl.json"
