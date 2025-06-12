#!/bin/bash

set -e  # Exit on error

# one of {dev, prod}
ENV=${ENV:-dev}

MAX_ZIP_SIZE_MB=25

SOURCE_DIR="smartscore"
OUTPUT_DIR="output"

STACK_NAME="SmartScore-$ENV"
TEMPLATE_FILE="templates/template.yaml"
PROCESSED_TEMPLATE_FILE="$OUTPUT_DIR/template.processed.yaml"
GET_ALL_PLAYERS_ASL_JSON_FILE="templates/get_all_players.asl.json"
GET_PLAYERS_ASL_JSON_FILE="templates/get_players.asl.json"

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

generate_preprocessed_template() {
  echo "Generating preprocessed CloudFormation template..."

  if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_API_KEY" ]; then
    echo "Error: SUPABASE_URL or SUPABASE_API_KEY environment variables are not set."
    exit 1
  fi

  # Create temporary files for the JSON content
  TEMP_ALL_PLAYERS_FILE=$(mktemp)
  TEMP_PLAYERS_FILE=$(mktemp)
  
  # Get pretty JSON and write to temp files with proper indentation
  echo "      DefinitionString: !Sub |" > "$TEMP_ALL_PLAYERS_FILE"
  jq . "$GET_ALL_PLAYERS_ASL_JSON_FILE" | sed 's/^/        /' >> "$TEMP_ALL_PLAYERS_FILE"
  
  echo "      DefinitionString: !Sub |" > "$TEMP_PLAYERS_FILE"
  jq . "$GET_PLAYERS_ASL_JSON_FILE" | sed 's/^/        /' >> "$TEMP_PLAYERS_FILE"

  cp "$TEMPLATE_FILE" "$PROCESSED_TEMPLATE_FILE"
  
  # Use awk for safer replacement that doesn't have delimiter issues
  awk '
  BEGIN { 
    while ((getline line < "'$TEMP_ALL_PLAYERS_FILE'") > 0) {
      all_players_content = all_players_content line "\n"
    }
    close("'$TEMP_ALL_PLAYERS_FILE'")
    
    while ((getline line < "'$TEMP_PLAYERS_FILE'") > 0) {
      players_content = players_content line "\n"
    }
    close("'$TEMP_PLAYERS_FILE'")
  }
  {
    if ($0 ~ /__GET_ALL_PLAYERS_JSON__/) {
      printf "%s", all_players_content
    } else if ($0 ~ /__GET_PLAYERS_JSON__/) {
      printf "%s", players_content
    } else {
      print $0
    }
  }
  ' "$PROCESSED_TEMPLATE_FILE" > "$PROCESSED_TEMPLATE_FILE.tmp" && mv "$PROCESSED_TEMPLATE_FILE.tmp" "$PROCESSED_TEMPLATE_FILE"
  
  # Clean up temp files
  rm -f "$TEMP_ALL_PLAYERS_FILE" "$TEMP_PLAYERS_FILE"

  cat "$PROCESSED_TEMPLATE_FILE"
}

generate_smartscore_stack() {
  if aws cloudformation describe-stacks --stack-name "$STACK_NAME" &>/dev/null; then
    echo "Updating CloudFormation stack $STACK_NAME..."
    UPDATE_OUTPUT=$(aws cloudformation update-stack \
      --stack-name "$STACK_NAME" \
      --template-body file://"$PROCESSED_TEMPLATE_FILE" \
      --parameters \
        ParameterKey=ENV,ParameterValue="$ENV" \
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
      --template-body file://"$PROCESSED_TEMPLATE_FILE" \
      --parameters \
        ParameterKey=ENV,ParameterValue="$ENV" \
        ParameterKey=SupabaseUrl,ParameterValue="$SUPABASE_URL" \
        ParameterKey=SupabaseApiKey,ParameterValue="$SUPABASE_API_KEY" \
      --capabilities CAPABILITY_NAMED_IAM

    echo "Waiting for CloudFormation stack creation to complete..."
    aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"
  fi

  STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].StackStatus" --output text)

  if [[ "$STACK_STATUS" != "CREATE_COMPLETE" && "$STACK_STATUS" != "UPDATE_COMPLETE" ]]; then
    echo "CloudFormation stack operation failed with status: $STACK_STATUS."
    exit 1
  fi

  echo "CloudFormation stack $STACK_NAME completed successfully with status: $STACK_STATUS."
}

generate_zip_file() {
  echo "Creating ZIP package for Lambda..."
  cd $OUTPUT_DIR
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
      --zip-file fileb://$OUTPUT_DIR/$KEY &>/dev/null

    if [ $? -ne 0 ]; then
      echo "Error: Failed to update Lambda function code: $FUNCTION."
      exit 1
    fi

    echo "Lambda function code updated successfully: $FUNCTION."
  done
}

# main

echo "Creating output directory..."
rm -rf $OUTPUT_DIR
mkdir -p $OUTPUT_DIR

echo "Updating dependencies..."
poetry export -f requirements.txt --output $OUTPUT_DIR/requirements.txt --without-hashes
poetry run pip install --no-deps -r $OUTPUT_DIR/requirements.txt -t $OUTPUT_DIR
rm -f $OUTPUT_DIR/requirements.txt

echo "Compiling C code..."
sh build_scripts/compile.sh || { echo "C compilation failed."; exit 1; }

echo "Compiling Rust code..."
sh build_scripts/rust_compile.sh || { echo "Rust compilation failed."; exit 1; }

echo "Updating the code..."
cp -r $SOURCE_DIR/* $OUTPUT_DIR
cp -r $OUTPUT_DIR/Rust/make_predictions/target/x86_64-unknown-linux-gnu/release/libmake_predictions_rust.so $OUTPUT_DIR/make_predictions_rust.so
rm -rf $OUTPUT_DIR/C $OUTPUT_DIR/Rust

generate_zip_file
generate_preprocessed_template
generate_smartscore_stack
update_lambda_code
