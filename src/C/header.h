#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef struct {
    float gpg_weight;
    float last_5_gpg_weight;
    float hgpg_weight;
    float tgpg_weight;
    float otga_weight;
    float comp_weight;
    float home_weight;
} Weights;

typedef struct {
    int scored;
    float gpg;
    float last_5_gpg;
    float hgpg;
    float ppg;
    float otpm;
    float tgpg;
    float otga;
    float home;
} Stats;

typedef struct {
    float** stats;
    char** dates;
    int numRows;
    int numRowsToNorm;
} Data;

float getWeight(Weights*, int);
float getStat(Stats*, int);
void setStat(Stats*, int, float);

float calculateStat(Stats*, Weights);

Data getData(char*);
Data normalizeData(Data);
float** allocateCurStats(Data);