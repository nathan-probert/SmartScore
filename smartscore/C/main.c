#include "header.h"

// Function to normalize player statistics
void normalize_stats(
    PlayerInfo* players, int num_players,
    float min_gpg, float max_gpg,
    float min_five_gpg, float max_five_gpg,
    float min_hgpg, float max_hgpg,
    float min_tgpg, float max_tgpg,
    float min_otga, float max_otga
) {

    for (int i = 0; i < num_players; i++) {
        // Normalize each player's stats
        players[i].gpg = (players[i].gpg - min_gpg) / (max_gpg - min_gpg);
        players[i].five_gpg = (players[i].five_gpg - min_five_gpg) / (max_five_gpg - min_five_gpg);
        players[i].hgpg = (players[i].hgpg - min_hgpg) / (max_hgpg - min_hgpg);
        players[i].tgpg = (players[i].tgpg - min_tgpg) / (max_tgpg - min_tgpg);
        players[i].otga = (players[i].otga - min_otga) / (max_otga - min_otga);
    }
}

// Function to calculate probabilities based on normalized stats
void calculate_probabilities(PlayerInfo* players, int num_players, float* probabilities) {
    // Define weight constants for the attributes
    const float GPG_WEIGHT = 0.3;        // Weight for goals per game
    const float FIVE_GPG_WEIGHT = 0.4;   // Weight for last 5 games goals per game
    const float HGPG_WEIGHT = 0.3;       // Weight for historical goals per game
    const float TGPG_WEIGHT = 0.0;       // Weight for team goals per game
    const float OTGA_WEIGHT = 0.0;       // Weight for other team goals average

    // Calculate probabilities using normalized stats
    for (int i = 0; i < num_players; i++) {
        // Initialize the probability for the current player based on all normalized stats
        probabilities[i] = (players[i].gpg * GPG_WEIGHT) +
                           (players[i].five_gpg * FIVE_GPG_WEIGHT) +
                           (players[i].hgpg * HGPG_WEIGHT) +
                           (players[i].tgpg * TGPG_WEIGHT) +
                           (players[i].otga * OTGA_WEIGHT);
    }
}

// Main function or calling function to execute the calculations
void process_players(PlayerInfo* players, int num_players, MinMax min_max, float* probabilities) {

    // Step 1: Normalize stats
    normalize_stats(
        players, num_players,
        min_max.min_gpg, min_max.max_gpg,
        min_max.min_five_gpg, min_max.max_five_gpg,
        min_max.min_hgpg, min_max.max_hgpg,
        min_max.min_tgpg, min_max.max_tgpg,
        min_max.min_otga, min_max.max_otga
    );

    // Step 2: Calculate probabilities using normalized stats
    calculate_probabilities(players, num_players, probabilities);
}
