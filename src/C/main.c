#include "header.h"

float** normalize(char* date) {
  FILE *file = fopen("./lib/data.csv", "r");
  if (file == NULL) {
    printf("Error opening file\n");
    exit(1);
  }

  // Count the number of rows in the file, also count number of rows to normalize
  int numRows = 0;
  int numRowsToNorm = 0;
  char line[1028];

  fgets(line, sizeof(line), file); // Skip the header line
  while (fgets(line, sizeof(line), file) != NULL) {
    char *token = strtok(line, ",");
    if (token != NULL) {
      numRows++;
      if (strcmp(token, date) == 0) {
        numRowsToNorm++;
      }
    }
  }
  rewind(file);

  // Allocate memory for the data array dynamically (8 stats to look at, 1 column for scored)
  float **stats = (float **)malloc(numRows * sizeof(float *));
  if (stats == NULL) {
    printf("Memory allocation failed\n");
    exit(1);
  }
  for (int i = 0; i < numRows; i++) {
    stats[i] = (float *)malloc(9 * sizeof(float));
    if (stats[i] == NULL) {
      printf("Memory allocation failed\n");
      // Free previously allocated memory before returning
      for (int j = 0; j < i; j++) {
        free(stats[j]);
      }
      free(stats);
      exit(1);
    }
  }

  // Read the data from the file into the data array
  fgets(line, sizeof(line), file);
  int curLine = 0;
  for (int i = 0; i < numRows; i++) {
    fgets(line, sizeof(line), file);
    char *token = strtok(line, ",");

    // skip date
    token = strtok(NULL, ",");
    
    // get scored
    stats[curLine][0]= atoi(token);

    // skip name id team and bet
    for (int j=0; j<4; j++) {
      token = strtok(NULL, ",");
    }

    // get gpg
    token = strtok(NULL, ",");
    stats[curLine][1] = atof(token);

    // get last 5 gpg
    token = strtok(NULL, ",");
    stats[curLine][2] = atof(token);

    // get hgpg
    token = strtok(NULL, ",");
    stats[curLine][3] = atof(token);
    
    // get ppg
    token = strtok(NULL, ",");
    stats[curLine][4] = atoi(token);

    // get otpm
    token = strtok(NULL, ",");
    stats[curLine][5] = atoi(token);

    // get tgpg
    token = strtok(NULL, ",");
    stats[curLine][6] = atof(token);

    // get otga
    token = strtok(NULL, ",");
    stats[curLine][7] = atof(token);

    // get home
    token = strtok(NULL, ",");
    stats[curLine][8] = atof(token);

    curLine++;
  }
  // Close the file
  fclose(file);

  // Normalize the data array
  // Scale the data using Min-Max scaling
  // up to 8 to skip home (already 0 and 1)

  for (int j = 1; j < 8; j++) {
    float min = stats[0][j];
    float max = stats[0][j];
    
    // Find the minimum and maximum values in the column
    for (int i = 0; i < numRows; i++) {
      if (stats[i][j] < min) {
        min = stats[i][j];
      }
      if (stats[i][j] > max) {
        max = stats[i][j];
      }
    }
    
    // Normalize the values in the column

    // set the last n elements to the types we want
    for (int i = (numRows-numRowsToNorm); i < numRows; i++) {
      stats[i][j] = ((stats[i][j] - min) / (max - min));
    }
  }

  // Allocate memory for curStats with numRowsToNorm rows and 7 columns
  float **curStats = (float **)malloc(numRowsToNorm * sizeof(float *));
  if (curStats == NULL) {
    printf("Memory allocation failed\n");
    exit(1);
  }

  for (int i = 0; i < numRowsToNorm; i++) {
    // Allocate memory for each row of curStats
    curStats[i] = (float *)malloc(7 * sizeof(float));
    if (curStats[i] == NULL) {
      printf("Memory allocation failed\n");
      // Free previously allocated memory before returning
      for (int j = 0; j < i; j++) {
        free(curStats[j]);
      }
      free(curStats);
      exit(1);
    }

    // Copy normalized statistics from stats to curStats
    for (int j = 0; j < 7; j++) {
      curStats[i][j] = stats[i + (numRows - numRowsToNorm)][j + 1];
    }
  }

  for (int i = 0; i < numRows; i++) {
    free(stats[i]);
  }

  return curStats;
}

float calculateStat(Stats* stats, Weights weights) {
  float probability = 0;

  float ratio = 0.18;
  float composite = ratio * stats->ppg + (1 - ratio) * stats->otpm;

  probability += stats->gpg * weights.gpg_weight;
  probability += stats->last_5_gpg * weights.last_5_gpg_weight;
  probability += stats->hgpg * weights.hgpg_weight;
  probability += stats->tgpg * weights.tgpg_weight;
  probability += stats->otga * weights.otga_weight;
  probability += composite * weights.comp_weight;
  probability += stats->home * weights.home_weight;

  // Apply the sigmoid function?
  // probability = 1 / (1 + exp(-probability));

  return probability;
}

float** getData() {
  FILE *file = fopen("./lib/data.csv", "r");
  if (file == NULL) {
    printf("Error opening file\n");
    exit(1);
  }

  // Count the number of rows in the file, also count number of rows to normalize
  int numRows = 0;
  int numRowsToNorm = 0;
  char line[1028];

  fgets(line, sizeof(line), file); // Skip the header line
  while (fgets(line, sizeof(line), file) != NULL) {
    char *token = strtok(line, ",");
    token = strtok(NULL, ","); // Get the 'Scored' column
    if (token != NULL) {
      numRows++;
      if (token[0] != '0' && token[0] != '1') {
        numRowsToNorm++;
      }
    }
  }
  rewind(file);

  // Allocate memory for the data array dynamically (8 stats to look at, 1 column for scored)
  float **stats = (float **)malloc(numRowsToNorm * sizeof(float *));
  if (stats == NULL) {
    printf("Memory allocation failed\n");
    exit(1);
  }
  for (int i = 0; i < numRowsToNorm; i++) {
    stats[i] = (float *)malloc(9 * sizeof(float));
    if (stats[i] == NULL) {
      printf("Memory allocation failed\n");
      // Free previously allocated memory before returning
      for (int j = 0; j < i; j++) {
        free(stats[j]);
      }
      free(stats);
      exit(1);
    }
  }

  // Read the data from the file into the data array
  fgets(line, sizeof(line), file);
  int curLine = 0;
  for (int i = 0; i < numRows; i++) {
    fgets(line, sizeof(line), file);
    if (i >= numRows-numRowsToNorm) {
      char *token = strtok(line, ",");

      // skip date
      token = strtok(NULL, ",");
      
      // get scored
      stats[curLine][0]= atoi(token);

      // skip name id team and bet
      for (int j=0; j<4; j++) {
        token = strtok(NULL, ",");
      }

      // get gpg
      token = strtok(NULL, ",");
      stats[curLine][1] = atof(token);

      // get last 5 gpg
      token = strtok(NULL, ",");
      stats[curLine][2] = atof(token);

      // get hgpg
      token = strtok(NULL, ",");
      stats[curLine][3] = atof(token);
      
      // get ppg
      token = strtok(NULL, ",");
      stats[curLine][4] = atoi(token);

      // get otpm
      token = strtok(NULL, ",");
      stats[curLine][5] = atoi(token);

      // get tgpg
      token = strtok(NULL, ",");
      stats[curLine][6] = atof(token);

      // get otga
      token = strtok(NULL, ",");
      stats[curLine][7] = atof(token);

      // get home
      token = strtok(NULL, ",");
      stats[curLine][8] = atof(token);

      curLine++;
    }
  }
  // Close the file
  fclose(file);

  return stats;
}

float* predictWeights(float** stats, int numRows) {
  Weights weights = {0.100000, 0.100000, 0.400000, 0.100000, 0.000000, 0.200000, 0.100000};
  float* probabilities = (float *)malloc(numRows * sizeof(float));
  for (int i = 0; i < numRows; i++) {
    Stats curStats = {stats[i][0], stats[i][1], stats[i][2], stats[i][3], stats[i][4], stats[i][5], stats[i][6], stats[i][7], stats[i][8]};
    probabilities[i] = calculateStat(&curStats, weights);
  }

  return probabilities;
}
