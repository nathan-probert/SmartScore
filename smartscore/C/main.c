#include "header.h"

// Function to find min and max values from all players
void find_min_max_from_all(
    PlayerInfo* all_players, int num_all_players,
    float* min_gpg, float* max_gpg,
    float* min_five_gpg, float* max_five_gpg,
    float* min_hgpg, float* max_hgpg,
    float* min_tgpg, float* max_tgpg,
    float* min_otga, float* max_otga
) {
    // Initialize min and max with the first player's stats
    *min_gpg = *max_gpg = all_players[0].gpg;
    *min_five_gpg = *max_five_gpg = all_players[0].five_gpg;
    *min_hgpg = *max_hgpg = all_players[0].hgpg;
    *min_tgpg = *max_tgpg = all_players[0].tgpg;
    *min_otga = *max_otga = all_players[0].otga;

    for (int i = 1; i < num_all_players; i++) { // Start from index 1

        // Update min and max values for each statistic
        if (all_players[i].gpg < *min_gpg) *min_gpg = all_players[i].gpg;
        if (all_players[i].gpg > *max_gpg) *max_gpg = all_players[i].gpg;

        if (all_players[i].five_gpg < *min_five_gpg) *min_five_gpg = all_players[i].five_gpg;
        if (all_players[i].five_gpg > *max_five_gpg) *max_five_gpg = all_players[i].five_gpg;

        if (all_players[i].hgpg < *min_hgpg) *min_hgpg = all_players[i].hgpg;
        if (all_players[i].hgpg > *max_hgpg) *max_hgpg = all_players[i].hgpg;

        if (all_players[i].tgpg < *min_tgpg) *min_tgpg = all_players[i].tgpg;
        if (all_players[i].tgpg > *max_tgpg) *max_tgpg = all_players[i].tgpg;

        if (all_players[i].otga < *min_otga) *min_otga = all_players[i].otga;
        if (all_players[i].otga > *max_otga) *max_otga = all_players[i].otga;
    }
}

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
    const float GPG_WEIGHT = 0.1;        // Weight for goals per game
    const float FIVE_GPG_WEIGHT = 0.0;   // Weight for last 5 games goals per game
    const float HGPG_WEIGHT = 0.8;       // Weight for historical goals per game
    const float TGPG_WEIGHT = 0.0;       // Weight for team goals per game
    const float OTGA_WEIGHT = 0.1;       // Weight for other team goals average

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
void process_players(PlayerInfo* players, int num_players, PlayerInfo* all_players, int num_all_players, float* probabilities) {

    // Variables to hold min and max values for normalization
    float min_gpg, max_gpg;
    float min_five_gpg, max_five_gpg;
    float min_hgpg, max_hgpg;
    float min_tgpg, max_tgpg;
    float min_otga, max_otga;

    // Step 1: Find min and max from all players for normalization
    find_min_max_from_all(
        all_players, num_all_players,
        &min_gpg, &max_gpg,
        &min_five_gpg, &max_five_gpg,
        &min_hgpg, &max_hgpg,
        &min_tgpg, &max_tgpg,
        &min_otga, &max_otga
    );

    printf("Min and Max Values:\n");
    printf("GPG: [%f, %f], FIVE_GPG: [%f, %f]\n", min_gpg, max_gpg, min_five_gpg, max_five_gpg);
    printf("HGPG: [%f, %f], TGPG: [%f, %f], OTGA: [%f, %f]\n", min_hgpg, max_hgpg, min_tgpg, max_tgpg, min_otga, max_otga);

    // Step 2: Normalize stats
    normalize_stats(
        players, num_players,
        min_gpg, max_gpg,
        min_five_gpg, max_five_gpg,
        min_hgpg, max_hgpg,
        min_tgpg, max_tgpg,
        min_otga, max_otga
    );

    // Step 3: Calculate probabilities using normalized stats
    calculate_probabilities(players, num_players, probabilities);
}
