#include "header.h"

float** normalize(char* date) {
  FILE *file = fopen("./lib/data.csv", "r");
  if (file == NULL) {
    fprintf(stderr, "Error opening file\n");
    exit(1);
  }

  Data data = getData(date);

  // Normalize the data array
  // Scale the data using Min-Max scaling
  // up to 8 to skip home (already 0 and 1)

  for (int j = 1; j < 8; j++) {
    float min = data.stats[0][j];
    float max = data.stats[0][j];
    
    // Find the minimum and maximum values in the column
    for (int i = 0; i < data.numRows; i++) {
      if (data.stats[i][j] < min) {
        min = data.stats[i][j];
      }
      if (data.stats[i][j] > max) {
        max = data.stats[i][j];
      }
    }
    
    // Normalize the values in the column
    // set the last n elements to the types we want
    for (int i = 0; i < data.numRows; i++) {
      data.stats[i][j] = ((data.stats[i][j] - min) / (max - min));
    }
  }

  // Allocate memory for curStats with numRowsToNorm rows and 7 columns
  float **curStats = (float **)malloc(data.numRowsNoScored * sizeof(float *));
  if (curStats == NULL) {
    fprintf(stderr, "Memory allocation failed\n");
    exit(1);
  }

  for (int i = 0; i < data.numRowsNoScored; i++) {
    // Allocate memory for each row of curStats
    curStats[i] = (float *)malloc(7 * sizeof(float));
    if (curStats[i] == NULL) {
      fprintf(stderr, "Memory allocation failed\n");
      // Free previously allocated memory before returning
      for (int j = 0; j < i; j++) {
        free(curStats[j]);
      }
      free(curStats);
      exit(1);
    }
  }

  int index = 0;
  for (int i = 0; i < data.numRows; i++) {
    // Copy normalized statistics from stats to curStats
    if (strcmp(data.dates[i], date) == 0) {
      for (int j = 0; j < 7; j++) {
        curStats[index][j] = data.stats[i][j + 1];
      }
      index++;
    }
  }

  for (int i = 0; i < data.numRows; i++) {
    free(data.stats[i]);
  }
  free(data.stats);

  for (int i = 0; i < data.numRows; i++) {
    free(data.dates[i]);
  }
  free(data.dates);

  return curStats;
}

Data getData(char* date) {
  FILE *file = fopen("./lib/data.csv", "r");
  if (file == NULL) {
    fprintf(stderr, "Error opening file\n");
    exit(1);
  }

  // Count the number of rows in the file, also count number of rows without 0 or 1 in the 'Scored' column
  Data data;
  data.numRows = 0;
  data.numRowsNoScored = 0;
  char line[1028];

  fgets(line, sizeof(line), file); // Skip the header line
  while (fgets(line, sizeof(line), file) != NULL) {
    char *token = strtok(line, ",");
    if (token != NULL) {
      data.numRows++;
      if (strcmp(token, date) == 0) {
        data.numRowsNoScored++;
      }
    }
  }
  rewind(file);

  // Allocate memory for the data array dynamically (8 stats to look at, 1 column for scored)
  data.stats = (float **)malloc(data.numRows * sizeof(float *));
  data.dates = (char **)malloc(data.numRows * sizeof(char *));
  if (data.stats == NULL || data.dates == NULL) {
    fprintf(stderr, "Memory allocation failed\n");
    exit(1);
  }
  for (int i = 0; i < data.numRows; i++) {
    data.stats[i] = (float *)malloc(9 * sizeof(float));
    data.dates[i] = (char *)malloc(11 * sizeof(char));
    if (data.stats[i] == NULL || data.dates[i] == NULL) {
      fprintf(stderr, "Memory allocation failed\n");
      // Free previously allocated memory before returning
      for (int j = 0; j < i; j++) {
        free(data.stats[j]);
      }
      free(data.stats);

      for (int j = 0; j < i; j++) {
        free(data.dates[j]);
      }
      free(data.dates);

      exit(1);
    }
  }

  // Read the data from the file into the data array
  fgets(line, sizeof(line), file);
  for (int curLine = 0; curLine < data.numRows; curLine++) {
    fgets(line, sizeof(line), file);
    
    char *token = strtok(line, ",");

    // get date
    strcpy(data.dates[curLine], token);
    
    // get scored
    token = strtok(NULL, ",");
    data.stats[curLine][0]= atoi(token);

    // skip name id team and bet
    for (int j=0; j<4; j++) {
      token = strtok(NULL, ",");
    }

    // Loop through the remaining tokens and populate the data.stats array
    for (int j = 1; j < 9; j++) {
      token = strtok(NULL, ",");
      data.stats[curLine][j] = atof(token);
    }    
  }
  // Close the file
  fclose(file);

  return data;
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

int empTest(float threshold) {
  // get all data
  Data data = getData(NULL);
  for (int i = 0; i < 10; i++) {
    for (int j = 0; j < 9; j++) {
      printf("%f ", data.stats[i][j]);
    }
    printf("\n");
  }

  return 0;
}
