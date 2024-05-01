#include "header.h"

float getWeight(Weights* weights, int i) {
    switch(i) {
        case 0: return weights->gpg_weight;
        case 1: return weights->last_5_gpg_weight;
        case 2: return weights->hgpg_weight;
        case 3: return weights->tgpg_weight;
        case 4: return weights->otga_weight;
        case 5: return weights->comp_weight;
        case 6: return weights->home_weight;
    }
}

float getStat(Stats* stats, int i) {
   switch(i) {
    case 0: return stats->scored;
    case 1: return stats->gpg;
    case 2: return stats->last_5_gpg;
    case 3: return stats->hgpg;
    case 4: return stats->ppg;
    case 5: return stats->otpm;
    case 6: return stats->tgpg;
    case 7: return stats->otga;
    case 8: return stats->home;
   }
}

void setStat(Stats* stats, int i, float value) {
    switch(i) {
     case 0: stats->scored = value; break;
     case 1: stats->gpg = value; break;
     case 2: stats->last_5_gpg = value; break;
     case 3: stats->hgpg = value; break;
     case 4: stats->ppg = value; break;
     case 5: stats->otpm = value; break;
     case 6: stats->tgpg = value; break;
     case 7: stats->otga = value; break;
     case 8: stats->home = value; break;
    }
}