#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>

typedef struct {
    float gpg;
    float hgpg;
    float five_gpg;
    float tgpg;
    float otga;
    int scored;
} PlayerInfo;

typedef struct {
	int team_id;
	float tgpg;
	float otga;
} TeamInfo;

typedef struct {
	float min_gpg;
	float max_gpg;
	float min_hgpg;
	float max_hgpg;
	float min_five_gpg;
	float max_five_gpg;
	float min_tgpg;
	float max_tgpg;
	float min_otga;
	float max_otga;
} MinMax;

typedef struct {
	float gpg;
	float five_gpg;
	float hgpg;
	float tgpg;
	float otga;
} Weights;

typedef struct {
    Weights weights;
    float accuracy;
    int correct;
    int wrong;
    float threshold;
} WeightInfo;