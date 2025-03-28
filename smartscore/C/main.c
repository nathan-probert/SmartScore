#include "header.h"

// Function to normalize player statistics
void normalize_stats(
    PlayerInfo *players, int num_players,
    float min_gpg, float max_gpg,
    float min_five_gpg, float max_five_gpg,
    float min_hgpg, float max_hgpg,
    float min_tgpg, float max_tgpg,
    float min_otga, float max_otga,
    float min_hppg, float max_hppg,
    float min_otshga, float max_otshga)
{

    for (int i = 0; i < num_players; i++)
    {

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

// Function to normalize player statistics for testing
void extended_testing_normalize_stats(
    TestingPlayerInfo *players, int num_players,
    float min_gpg, float max_gpg,
    float min_five_gpg, float max_five_gpg,
    float min_hgpg, float max_hgpg,
    float min_tgpg, float max_tgpg,
    float min_otga, float max_otga,
    float min_hppg, float max_hppg,
    float min_otshga, float max_otshga)
{

    for (int i = 0; i < num_players; i++)
    {

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
void calculate_probabilities(PlayerInfo *players, int num_players, float *probabilities, Weights weights)
{
    for (int i = 0; i < num_players; i++)
    {
        probabilities[i] = (players[i].gpg * weights.gpg) +
                           (players[i].five_gpg * weights.five_gpg) +
                           (players[i].hgpg * weights.hgpg) +
                           (players[i].tgpg * weights.tgpg) +
                           (players[i].otga * weights.otga) +
                           (players[i].hppg_otshga * weights.hppg_otshga) +
                           (players[i].is_home * weights.is_home);
    }
}

// Main function to process given players and calculate probabilities
void process_players(PlayerInfo *players, int num_players, MinMax min_max, float *probabilities, Weights weights)
{
    // Step 1: Normalize stats
    printf("Normalizing stats...\n");
    normalize_stats(
        players, num_players,
        min_max.min_gpg, min_max.max_gpg,
        min_max.min_five_gpg, min_max.max_five_gpg,
        min_max.min_hgpg, min_max.max_hgpg,
        min_max.min_tgpg, min_max.max_tgpg,
        min_max.min_otga, min_max.max_otga,
        min_max.min_hppg, min_max.max_hppg,
        min_max.min_otshga, min_max.max_otshga);

    // Step 2: Calculate probabilities using normalized stats
    printf("Calculating probabilities...\n");
    calculate_probabilities(players, num_players, probabilities, weights);

    printf("Done calculating probabilities...\n");
}

// Function to check probabilities and return the number of correct predictions
int check_probabilities(TestingPlayerInfo *players, float *probabilities, int num_players)
{
    int correct = 0;

    int highest_prob_indices[3];
    float highest_prob_values[3];
    char last_date[20];
    strcpy(last_date, players[0].date);

    for (int i = 0; i < 3; i++)
        highest_prob_values[i] = -1.0f;

    for (int i = 0; i < num_players; i++)
    {
        if (strcmp(last_date, players[i].date) != 0 || i == num_players - 1)
        {
            for (int j = 0; j < 3; j++)
            {
                if (highest_prob_values[j] != -1.0f && players[highest_prob_indices[j]].scored)
                {
                    correct++;
                }
            }

            for (int j = 0; j < 3; j++)
                highest_prob_values[j] = -1.0f;

            strcpy(last_date, players[i].date);
        }

        if (probabilities[i] > highest_prob_values[(int)players[i].tims - 1])
        {
            highest_prob_values[(int)players[i].tims - 1] = probabilities[i];
            highest_prob_indices[(int)players[i].tims - 1] = i;
        }
    }

    return correct;
}

// Main function to test weights and print best results
void test_weights(TestingPlayerInfo *players, int num_players, MinMax min_max, float *probabilities, int num_tims_dates)
{
    Weights weights;
    Weights max_weights;
    int max_correct = -1;

    printf("Normalizing stats...\n");
    extended_testing_normalize_stats(players, num_players, min_max.min_gpg, min_max.max_gpg,
                                     min_max.min_five_gpg, min_max.max_five_gpg, min_max.min_hgpg, min_max.max_hgpg,
                                     min_max.min_tgpg, min_max.max_tgpg, min_max.min_otga, min_max.max_otga,
                                     min_max.min_hppg, min_max.max_hppg, min_max.min_otshga, min_max.max_otshga);

    printf("Calculating probabilities...\n");
    for (int gpg_weight = 0; gpg_weight <= 100; gpg_weight += STEP_SIZE)
    {
        for (int five_gpg_weight = 0; five_gpg_weight <= 100 - gpg_weight; five_gpg_weight += STEP_SIZE)
        {
            for (int hgpg_weight = 0; hgpg_weight <= 100 - gpg_weight - five_gpg_weight; hgpg_weight += STEP_SIZE)
            {
                for (int tgpg_weight = 0; tgpg_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight; tgpg_weight += STEP_SIZE)
                {
                    for (int otga_weight = 0; otga_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight; otga_weight += STEP_SIZE)
                    {
                        for (int hppg_otshga_weight = 0; hppg_otshga_weight <= 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight - otga_weight; hppg_otshga_weight += STEP_SIZE)
                        {
                            int is_home_weight = 100 - gpg_weight - five_gpg_weight - hgpg_weight - tgpg_weight - otga_weight - hppg_otshga_weight;

                            weights.gpg = gpg_weight / 100.0;
                            weights.five_gpg = five_gpg_weight / 100.0;
                            weights.hgpg = hgpg_weight / 100.0;
                            weights.tgpg = tgpg_weight / 100.0;
                            weights.otga = otga_weight / 100.0;
                            weights.hppg_otshga = hppg_otshga_weight / 100.0;
                            weights.is_home = is_home_weight / 100.0;

                            // force weights for single run
                            // weights.gpg = 0.3;
                            // weights.five_gpg = 0.4;
                            // weights.hgpg = 0.3;
                            // weights.tgpg = 0.0;
                            // weights.otga = 0.0;
                            // weights.hppg_otshga = 0.0;
                            // weights.is_home = 0.0;

                            for (int i = 0; i < num_players; i++)
                            {
                                probabilities[i] = (players[i].gpg * weights.gpg) +
                                                   (players[i].five_gpg * weights.five_gpg) +
                                                   (players[i].hgpg * weights.hgpg) +
                                                   (players[i].tgpg * weights.tgpg) +
                                                   (players[i].otga * weights.otga) +
                                                   (players[i].hppg_otshga * weights.hppg_otshga) +
                                                   (players[i].is_home * weights.is_home);
                            }

                            int correct = check_probabilities(players, probabilities, num_players);
                            if (correct >= max_correct)
                            {
                                max_correct = correct;
                                max_weights = weights;
                            }

                            // for single run
                            // break;
                        }
                    }
                }
            }
        }
    }

    printf("\nDone testing weights...\n");
    printf("Max correct: %d\n", max_correct);
    printf("Number of dates: %d\n", num_tims_dates);
    printf("Accuracy: %f\n", (float)max_correct / (num_tims_dates * 3));
    printf("Weights: gpg: %f, five_gpg: %f, hgpg: %f, tgpg: %f, otga: %f, hppg_otshga: %f, is_home: %f\n", max_weights.gpg, max_weights.five_gpg, max_weights.hgpg, max_weights.tgpg, max_weights.otga, max_weights.hppg_otshga, max_weights.is_home);
}
