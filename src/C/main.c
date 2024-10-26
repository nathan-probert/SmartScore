#include "header.h"

float** normalize(char* date) {
  FILE *file = fopen("./lib/data.csv", "r");
  if (file == NULL) {
    fprintf(stderr, "Error opening file\n");
    exit(1);
  }

  Data data = getData(date);

  // Normalize the data array using Min-Max scaling
  normalizeData(data);

  // Allocate memory for curStats with numRowsToNorm rows and 7 columns
  float **curStats = allocateCurStats(data);

  // Copy normalized statistics from stats to curStats for the given date
  int index = 0;
  for (int i = 0; i < data.numRows; i++) {
    if (strcmp(data.dates[i], date) == 0) {
      for (int j = 0; j < 7; j++) {
        curStats[index][j] = data.stats[i][j + 1];
      }
      index++;
    }
  }

  // free data and dates
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

float** allocateCurStats(Data data) {
  float **curStats = (float **)malloc(data.numRowsToNorm * sizeof(float *));
  if (curStats == NULL) {
    fprintf(stderr, "Memory allocation failed\n");
    exit(1);
  }

  for (int i = 0; i < data.numRowsToNorm; i++) {
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

  return curStats;
}

Data normalizeData(Data data) {
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

  return data;
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
  data.numRowsToNorm = 0;
  char line[1028];

  fgets(line, sizeof(line), file); // Skip the header line
  while (fgets(line, sizeof(line), file) != NULL) {
    char *token = strtok(line, ",");
    if (token != NULL) {
      data.numRows++;
      if (strcmp(token, date) == 0) {
        data.numRowsToNorm++;
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

Data getNormalizedData(char* date) {
  Data data = getData(date);
  normalizeData(data);
  return data;
}

float* predictGivenWeights(float** stats, int numRows, Weights weights, int fromPredictWeights) {
  float* probabilities = (float *)malloc(numRows * sizeof(float));
  for (int i = 0; i < numRows; i++) {
    Stats curStats = {0, stats[i][1-fromPredictWeights], stats[i][2-fromPredictWeights], stats[i][3-fromPredictWeights], stats[i][4-fromPredictWeights], stats[i][5-fromPredictWeights], stats[i][6-fromPredictWeights], stats[i][7-fromPredictWeights], stats[i][8-fromPredictWeights]};
    probabilities[i] = calculateStat(&curStats, weights);
  }

  return probabilities;
}

float* predictWeights(float** stats, int numRows) {
  // old best weights
  Weights weights = {0.300000, 0.300000, 0.000000, 0.000000, 0.200000, 0.200000, 0.000000};

  // most up to date weights
  // Weights weights = {0.300000, 0.100000, 0.000000, 0.000000, 0.100000, 0.500000, 0.000000};

  // best weights with sigmoid function
  // Weights weights = {0.000000, 0.100000, 0.300000, 0.000000, 0.100000, 0.500000, 0.000000};

  return predictGivenWeights(stats, numRows, weights, 1);
}

// Function to print a progress bar
void printProgressBar(int iteration, int total) {
    int progress = (int)((float)iteration / total * 100);
    char progressBar[52];
    sprintf(progressBar, "[");
    for (int i = 0; i < 50; i++) {
      if (i < progress / 2)
        sprintf(progressBar + strlen(progressBar), "=");
      else if (i == progress / 2)
        sprintf(progressBar + strlen(progressBar), ">");
      else
        sprintf(progressBar + strlen(progressBar), " ");
    }
    sprintf(progressBar + strlen(progressBar), "] %d%%\r", progress);
    printf("%s", progressBar);
    fflush(stdout); // Flush output to display progress immediately
}

int empTest(float threshold, char* date) {
  // get all data with today's date as the normalized data (to count it)
  Data data = getData(date);
  normalizeData(data);

  int rowsWithScored = data.numRows-data.numRowsToNorm;
  printf("Number of rows: %d\n", rowsWithScored);

  int bestTotalCount = 0;
  int bestCorrectCount = 0;
  float bestRatio = 0.0;
  Weights bestWeights;

  int totalWeight = 10;

  int iterations = 0;
  int superTotal = 0;
  if (totalWeight == 10) {
    superTotal = 8008;
  } else if (totalWeight == 100) {
    superTotal = 1705904746;
  }

  float bestThreshold = 0.0;
  int bestThresTotalCount = 0;
  int bestThresCorrectCount = 0;
  float bestThresRatio = 0.0;
  int thresLoop = 1;
  if (threshold == -1) {
    thresLoop = 100;
    superTotal *= 100;
    threshold = 0.01;
  }

  for (int i=0; i<thresLoop; i++) {
    for (int gpgWeight = 0; gpgWeight <= totalWeight; gpgWeight++) {
      int remainingWeight1 = totalWeight - gpgWeight;

      for (int last5GpgWeight = 0; last5GpgWeight <= remainingWeight1; last5GpgWeight++) {
        int remainingWeight2 = remainingWeight1 - last5GpgWeight;

        for (int hgpgWeight = 0; hgpgWeight <= remainingWeight2; hgpgWeight++) {
          int remainingWeight3 = remainingWeight2 - hgpgWeight;

          for (int tgpgWeight = 0; tgpgWeight <= remainingWeight3; tgpgWeight++) {
            int remainingWeight4 = remainingWeight3 - tgpgWeight;

            for (int otgaWeight = 0; otgaWeight <= remainingWeight4; otgaWeight++) {
              int remainingWeight5 = remainingWeight4 - otgaWeight;
              
              for (int compWeight = 0; compWeight <= remainingWeight5; compWeight++) {
                int homeWeight = remainingWeight5 - compWeight;
                
                Weights weights = {
                  (float)gpgWeight / totalWeight,
                  (float)last5GpgWeight / totalWeight,
                  (float)hgpgWeight / totalWeight,
                  (float)tgpgWeight / totalWeight,
                  (float)otgaWeight / totalWeight,
                  (float)compWeight / totalWeight,
                  (float)homeWeight / totalWeight,
                };

                int totalCount = 0;
                int correctCount = 0;

                float* probabilities = predictGivenWeights(data.stats, data.numRows, weights, 0);

                for (int i=0; i<rowsWithScored; i++) {
                  // Stats curStats = {data.stats[i][0], data.stats[i][1], data.stats[i][2], data.stats[i][3], data.stats[i][4], data.stats[i][5], data.stats[i][6], data.stats[i][7], data.stats[i][8]};
                  // float probability = calculateStat(&curStats, weights);

                  float probability = probabilities[i];

                  if (probability >= threshold) {
                    totalCount++;
                    if (data.stats[i][0] == 1) {
                      correctCount++;
                    }
                  }
                }
                free(probabilities);

                float ratio = 0;
                if (totalCount != 0) {
                  ratio = (float)correctCount / totalCount;

                  if (ratio > bestRatio) {
                    bestTotalCount = totalCount;
                    bestCorrectCount = correctCount;
                    bestRatio = ratio;
                    bestWeights = weights;
                  }
                }

                // // display the ratio
                // printf("%d \\ %d : %f - ", correctCount, totalCount, ratio);
                // for (int i = 0; i < 7; i++) {
                //     printf("%f, ", getWeight(&weights, i));
                // }
                // printf("\n");

                iterations++;
              }
              printProgressBar(iterations, superTotal);
            }
          }
        }
      }
    }

    if (bestRatio > bestThresRatio) {
      bestThresTotalCount = bestTotalCount;
      bestThresCorrectCount = bestCorrectCount;
      bestThresRatio = bestRatio;
      bestThreshold = threshold;
    }

    threshold += 0.01;
  }

  printf("Highest weights: {");
  for (int i = 0; i < 7; i++) {
      printf("%f", getWeight(&bestWeights, i));
      if (i < 6) {
          printf(", ");
      }
  }
  printf("}, with %d \\ %d = %f\n", bestCorrectCount, bestTotalCount, bestRatio);

  printf("Best threshold: %f, with %d \\ %d = %f\n", bestThreshold, bestThresCorrectCount, bestThresTotalCount, bestThresRatio);

  return 0;
}
