#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Constants
#define STEP_SIZE 2.5

// Structs
typedef struct
{
    float gpg;
    float hgpg;
    float five_gpg;
    float tgpg;
    float otga;
    float hppg;
    float otshga;
    float is_home;
    float hppg_otshga;
} PlayerInfo;

typedef struct
{
    float gpg;
    float hgpg;
    float five_gpg;
    float tgpg;
    float otga;
    float hppg;
    float otshga;
    float is_home;
    float hppg_otshga;
    float scored;
    float tims;
    char *date;
} TestingPlayerInfo;

typedef struct
{
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
    float min_hppg;
    float max_hppg;
    float min_otshga;
    float max_otshga;
} MinMax;

typedef struct
{
    float gpg;
    float hgpg;
    float five_gpg;
    float tgpg;
    float otga;
    float is_home;
    float hppg_otshga;
} Weights;