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


// Function to normalize player statistics
void extended_normalize_stats(
    PlayerInfo* players, int num_players,
    float min_gpg, float max_gpg,
    float min_five_gpg, float max_five_gpg,
    float min_hgpg, float max_hgpg,
    float min_tgpg, float max_tgpg,
    float min_otga, float max_otga,
    float min_hppg, float max_hppg,
    float min_otshga, float max_otshga
) {

    for (int i = 0; i < num_players; i++) {

        // Normalize each player's stats
        players[i].gpg = (players[i].gpg - min_gpg) / (max_gpg - min_gpg);
        players[i].five_gpg = (players[i].five_gpg - min_five_gpg) / (max_five_gpg - min_five_gpg);
        players[i].hgpg = (players[i].hgpg - min_hgpg) / (max_hgpg - min_hgpg);
        players[i].tgpg = (players[i].tgpg - min_tgpg) / (max_tgpg - min_tgpg);
        players[i].otga = (players[i].otga - min_otga) / (max_otga - min_otga);
        players[i].hppg = (players[i].hppg - min_hppg) / (max_hppg - min_hppg);
        players[i].otshga = (players[i].otshga - min_otshga) / (max_otshga - min_otshga);
        players[i].hppg_otshga = (players[i].hppg * players[i].otshga);
    }
}


void extended_testing_normalize_stats(
    TestingPlayerInfo* players, int num_players,
    float min_gpg, float max_gpg,
    float min_five_gpg, float max_five_gpg,
    float min_hgpg, float max_hgpg,
    float min_tgpg, float max_tgpg,
    float min_otga, float max_otga,
    float min_hppg, float max_hppg,
    float min_otshga, float max_otshga
) {

    for (int i = 0; i < num_players; i++) {

        // Normalize each player's stats
        players[i].gpg = (players[i].gpg - min_gpg) / (max_gpg - min_gpg);
        players[i].five_gpg = (players[i].five_gpg - min_five_gpg) / (max_five_gpg - min_five_gpg);
        players[i].hgpg = (players[i].hgpg - min_hgpg) / (max_hgpg - min_hgpg);
        players[i].tgpg = (players[i].tgpg - min_tgpg) / (max_tgpg - min_tgpg);
        players[i].otga = (players[i].otga - min_otga) / (max_otga - min_otga);
        players[i].hppg = (players[i].hppg - min_hppg) / (max_hppg - min_hppg);
        players[i].otshga = (players[i].otshga - min_otshga) / (max_otshga - min_otshga);
        players[i].hppg_otshga = (players[i].hppg * players[i].otshga);
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


// Function to calculate probabilities based on normalized stats
void extended_calculate_probabilities(PlayerInfo* players, int num_players, float* probabilities) {
    // Define weight constants for the attributes
    const float GPG_WEIGHT = 0.3;        // Weight for goals per game
    const float FIVE_GPG_WEIGHT = 0.4;   // Weight for last 5 games goals per game
    const float HGPG_WEIGHT = 0.3;       // Weight for historical goals per game
    const float TGPG_WEIGHT = 0.0;       // Weight for team goals per game
    const float OTGA_WEIGHT = 0.0;       // Weight for other team goals average
    const float HPPG_OTSHGA_WEIGHT = 0.0;       // Weight for historical powerplay goals per game and other team short handed goals against
    const float HOME_WEIGHT = 0.0;       // Weight for home games

    // Calculate probabilities using normalized stats
    for (int i = 0; i < num_players; i++) {
        // Initialize the probability for the current player based on all normalized stats
        probabilities[i] = (players[i].gpg * GPG_WEIGHT) +
                           (players[i].five_gpg * FIVE_GPG_WEIGHT) +
                           (players[i].hgpg * HGPG_WEIGHT) +
                           (players[i].tgpg * TGPG_WEIGHT) +
                           (players[i].otga * OTGA_WEIGHT) +
                           (players[i].hppg_otshga * HPPG_OTSHGA_WEIGHT) +
                           (players[i].is_home * HOME_WEIGHT);
    }
}


// Main function or calling function to execute the calculations
void process_players(PlayerInfo* players, int num_players, MinMax min_max, float* probabilities) {

    // Step 1: Normalize stats
    printf("Normalizing stats...\n");
    normalize_stats(
        players, num_players,
        min_max.min_gpg, min_max.max_gpg,
        min_max.min_five_gpg, min_max.max_five_gpg,
        min_max.min_hgpg, min_max.max_hgpg,
        min_max.min_tgpg, min_max.max_tgpg,
        min_max.min_otga, min_max.max_otga
    );

    // Step 2: Calculate probabilities using normalized stats
    printf("Calculating probabilities...\n");
    calculate_probabilities(players, num_players, probabilities);

    printf("Done calculating probabilities...\n");
}


// Main function or calling function to execute the calculations
void extended_process_players(PlayerInfo* players, int num_players, MinMax min_max, float* probabilities) {

    // Step 1: Normalize stats
    printf("Normalizing stats...\n");
    extended_normalize_stats(
        players, num_players,
        min_max.min_gpg, min_max.max_gpg,
        min_max.min_five_gpg, min_max.max_five_gpg,
        min_max.min_hgpg, min_max.max_hgpg,
        min_max.min_tgpg, min_max.max_tgpg,
        min_max.min_otga, min_max.max_otga,
        min_max.min_hppg, min_max.max_hppg,
        min_max.min_otshga, min_max.max_otshga
    );

    // Step 2: Calculate probabilities using normalized stats
    printf("Calculating probabilities...\n");
    extended_calculate_probabilities(players, num_players, probabilities);
}

int check_probabilities(TestingPlayerInfo* players, float* probabilities, int num_players) {
    int MAX_DATES = 100;
    int correct = 0;

    int highest_prob_indices[MAX_DATES][3];
    float highest_prob_values[MAX_DATES][3];
    char dates[MAX_DATES][20];
    int date_count = 0;

    for (int i = 0; i < MAX_DATES; i++)
        for (int j = 0; j < 3; j++)
            highest_prob_values[i][j] = -1.0f;

    for (int i = 0; i < num_players; i++) {
        if (players[i].tims >= 1 && players[i].tims <= 3) {
            int date_index = -1;

            for (int j = 0; j < date_count; j++) {
                if (strcmp(dates[j], players[i].date) == 0) {
                    date_index = j;
                    break;
                }
            }

            if (date_index == -1 && date_count < MAX_DATES) {
                strcpy(dates[date_count], players[i].date);
                date_index = date_count;
                date_count++;
            }

            if (date_index != -1 && probabilities[i] > highest_prob_values[date_index][(int)players[i].tims - 1]) {
                highest_prob_values[date_index][(int)players[i].tims - 1] = probabilities[i];
                highest_prob_indices[date_index][(int)players[i].tims - 1] = i;
            }
        }
    }

    for (int i = 0; i < date_count; i++) {
        for (int j = 0; j < 3; j++) {
            if (highest_prob_values[i][j] != -1.0f && players[highest_prob_indices[i][j]].scored) {
                correct++;
            }
        }
    }

    return correct;
}

void test_weights(TestingPlayerInfo* players, int num_players, MinMax min_max, float* probabilities, int num_tims_dates) {
    Weights weights;
    Weights max_weights;
    int max_correct = -1;

    printf("Normalizing stats...\n");
    extended_testing_normalize_stats(players, num_players, min_max.min_gpg, min_max.max_gpg,
                             min_max.min_five_gpg, min_max.max_five_gpg, min_max.min_hgpg, min_max.max_hgpg,
                             min_max.min_tgpg, min_max.max_tgpg, min_max.min_otga, min_max.max_otga,
                             min_max.min_hppg, min_max.max_hppg, min_max.min_otshga, min_max.max_otshga);

    const int STEP = 10;
    for (int gpg_weight = 0; gpg_weight <= 100; gpg_weight+=STEP) {
        for (int five_gpg_weight = 0; five_gpg_weight <= 100 - gpg_weight; five_gpg_weight+=STEP) {
            for (int hgpg_weight = 0; hgpg_weight <= 100 - gpg_weight - five_gpg_weight; hgpg_weight+=STEP) {
                for (int tgpg_weight = 0; tgpg_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight; tgpg_weight+=STEP) {
                    for (int otga_weight = 0; otga_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight; otga_weight+=STEP) {
                        for (int hppg_otshga_weight = 0; hppg_otshga_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight - otga_weight; hppg_otshga_weight+=STEP) {
                            int is_home_weight = 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight - otga_weight - hppg_otshga_weight;
                            weights.gpg = gpg_weight / 100.0;
                            weights.five_gpg = five_gpg_weight / 100.0;
                            weights.hgpg = hgpg_weight / 100.0;
                            weights.tgpg = tgpg_weight / 100.0;
                            weights.otga = otga_weight / 100.0;
                            weights.hppg_otshga = hppg_otshga_weight / 100.0;
                            weights.is_home = is_home_weight / 100.0;

                            for (int i = 0; i < num_players; i++) {
                                probabilities[i] = (players[i].gpg * weights.gpg) +
                                                   (players[i].five_gpg * weights.five_gpg) +
                                                   (players[i].hgpg * weights.hgpg) +
                                                   (players[i].tgpg * weights.tgpg) +
                                                   (players[i].otga * weights.otga) +
                                                   (players[i].hppg_otshga * weights.hppg_otshga) +
                                                   (players[i].is_home * weights.is_home);
                            }

                            int correct = check_probabilities(players, probabilities, num_players);
                            if (correct > max_correct) {
                                max_correct = correct;
                                max_weights = weights;
                            }
                        }
                    }
                }
            }
        }
    }

    printf("Max correct: %d\n", max_correct);
    printf("Number of dates: %d\n", num_tims_dates);
    printf("Accuracy: %f\n", (float)max_correct / (num_tims_dates*3));
    printf("Weights: gpg: %f, five_gpg: %f, hgpg: %f, tgpg: %f, otga: %f, hppg_otshga: %f, is_home: %f\n", max_weights.gpg, max_weights.five_gpg, max_weights.hgpg, max_weights.tgpg, max_weights.otga, max_weights.hppg_otshga, max_weights.is_home);
}
