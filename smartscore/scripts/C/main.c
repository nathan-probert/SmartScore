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
float calculate_probabilities_difference(PlayerInfo* players, int num_players, const Weights* weights) {
	float difference = 0.0;
	float *probabilities = (float*)malloc(num_players * sizeof(float));
	float min = 1.0;
	float max = 0.0;

    for (int i = 0; i < num_players; i++) {
        probabilities[i] = (players[i].gpg * weights->gpg) +
                           (players[i].five_gpg * weights->five_gpg) +
                           (players[i].hgpg * weights->hgpg) +
                           (players[i].tgpg * weights->tgpg) +
                           (players[i].otga * weights->otga);

		if (probabilities[i] < min) {
			min = probabilities[i];
		}
		if (probabilities[i] > max) {
			max = probabilities[i];
		}
	}

	for (int i = 0; i < num_players; i++) {
		if (players[i].scored == 1) {
			difference += (max - probabilities[i]);
		} else {
			difference += (probabilities[i] - min);
		}
	}


	free(probabilities);
	return difference;
}














// Function to calculate probabilities based on normalized stats
WeightInfo calculate_probabilities(PlayerInfo* players, int num_players, const Weights* weights, float threshold) {

	float *probabilities = (float*)malloc(num_players * sizeof(float));

	int wrong = 0;
	int correct = 0;

    // Calculate probabilities using normalized stats
    for (int i = 0; i < num_players; i++) {
        // Initialize the probability for the current player based on all normalized stats
        probabilities[i] = (players[i].gpg * weights->gpg) +
                           (players[i].five_gpg * weights->five_gpg) +
                           (players[i].hgpg * weights->hgpg) +
                           (players[i].tgpg * weights->tgpg) +
                           (players[i].otga * weights->otga);

        if (probabilities[i] > threshold) {
            if (players[i].scored == 1) {
				correct += 1;
			} else {
				wrong += 1;
			}
		}
    }
    free(probabilities);

	WeightInfo weightInfo;
	weightInfo.weights = *weights;
	weightInfo.correct = correct;
	weightInfo.wrong = wrong;
	weightInfo.threshold = threshold;
	weightInfo.accuracy = (float)correct/(correct+wrong);
    return weightInfo;
}


void set_weights(Weights* weights, float gpg, float five_gpg, float hgpg, float tgpg, float otga) {
    weights->gpg = gpg;
    weights->five_gpg = five_gpg;
    weights->hgpg = hgpg;
    weights->tgpg = tgpg;
    weights->otga = otga;
}


// Function to check if two weights are equal
bool are_weights_equal(Weights w1, Weights w2) {
    return (w1.gpg == w2.gpg) &&
           (w1.five_gpg == w2.five_gpg) &&
           (w1.hgpg == w2.hgpg) &&
           (w1.tgpg == w2.tgpg) &&
           (w1.otga == w2.otga);
}


// Function to find and print the most common weights
void print_most_common_weights(WeightInfo arr[], int size) {
    int count[1001] = {0};  // Array to count occurrences of each weight configuration
    Weights unique_weights[1001]; // Array to store unique weights
    int unique_count = 0;

    // Count occurrences of each weight configuration
    for (int i = 0; i < size; i++) {
        bool found = false;
        for (int j = 0; j < unique_count; j++) {
            if (are_weights_equal(arr[i].weights, unique_weights[j])) {
                count[j]++;
                found = true;
                break;
            }
        }
        if (!found) {
            unique_weights[unique_count] = arr[i].weights;
            count[unique_count] = 1;
            unique_count++;
        }
    }

    // Find the maximum occurrences
    int max_count = 0;
    for (int i = 0; i < unique_count; i++) {
        if (count[i] > max_count) {
            max_count = count[i];
        }
    }

    // Print the most common weights
    printf("Most common weights:\n");
    for (int i = 0; i < unique_count; i++) {
        if (count[i] == max_count) {
            printf("Weights: gpg=%.2f, five_gpg=%.2f, hgpg=%.2f, tgpg=%.2f, otga=%.2f, Count: %d\n",
                   unique_weights[i].gpg,
                   unique_weights[i].five_gpg,
                   unique_weights[i].hgpg,
                   unique_weights[i].tgpg,
                   unique_weights[i].otga,
                   max_count);
        }
    }
}


// Main function or calling function to execute the calculations
void process_players(PlayerInfo* players, int num_players, MinMax min_max) {

	printf("Processing player data for %d players...\n", num_players);

    // Step 1: Normalize stats
    normalize_stats(
        players, num_players,
        min_max.min_gpg, min_max.max_gpg,
        min_max.min_five_gpg, min_max.max_five_gpg,
        min_max.min_hgpg, min_max.max_hgpg,
        min_max.min_tgpg, min_max.max_tgpg,
        min_max.min_otga, min_max.max_otga
    );


    // menu
    printf("1. Empirical Testing for Threshold and Weights\n");
    printf("2. Test Single Weights\n");
    printf("3. Probability difference\n");
    int choice;
    printf("Enter choice: ");
    scanf("%d", &choice);


	if (choice == 1) {
		WeightInfo best_weights[100];

	    Weights weights;
	    for (int threshold = 0; threshold < 100; threshold++) {
			float threshold_value = threshold / 100.0f;

			WeightInfo weightInfo[1001];
			int i = 0;

		    for (int gpg = 0; gpg <= 10; gpg++) {
		        for (int five_gpg = 0; five_gpg <= 10 - gpg; five_gpg++) {
		            for (int hgpg = 0; hgpg <= 10 - gpg - five_gpg; hgpg++) {
		                for (int tgpg = 0; tgpg <= 10 - gpg - five_gpg - hgpg; tgpg++) {

		                    float gpg_value = gpg / 10.0f;
		                    float five_gpg_value = five_gpg / 10.0f;
		                    float hgpg_value = hgpg / 10.0f;
		                    float tgpg_value = tgpg / 10.0f;
		                    float otga_value = 1.0f - gpg_value - five_gpg_value - hgpg_value - tgpg_value;

		                    set_weights(&weights, gpg_value, five_gpg_value, hgpg_value, tgpg_value, otga_value);
		                    weightInfo[i] = calculate_probabilities(players, num_players, &weights, threshold_value);
		                    i+=1;
		                }
		            }
		        }
		    }


		    WeightInfo max_weightInfo = weightInfo[0];
		    for (int i = 1; i < 1001; i++) {
				if (weightInfo[i].accuracy > max_weightInfo.accuracy) {
					max_weightInfo = weightInfo[i];
				}
			}

			printf("Best weights: gpg=%.2f, five_gpg=%.2f, hgpg=%.2f, tgpg=%.2f, otga=%.2f\n", max_weightInfo.weights.gpg, max_weightInfo.weights.five_gpg, max_weightInfo.weights.hgpg, max_weightInfo.weights.tgpg, max_weightInfo.weights.otga);
			printf("Accuracy: %.2f%%, Total: %d, Threshold: %f\n", max_weightInfo.accuracy * 100, max_weightInfo.correct + max_weightInfo.wrong, max_weightInfo.threshold);
			printf("\n");
			best_weights[threshold] = max_weightInfo;
		}

		print_most_common_weights(best_weights, 100);
	} else if (choice == 2) {
		Weights weights;
		set_weights(&weights, 0.3, 0.4, 0.3, 0.0, 0.0);
		for (int i = 0; i < 100; i++) {
			WeightInfo weightInfo = calculate_probabilities(players, num_players, &weights, i/100.0f);
			printf("Accuracy: %.2f%%, Total: %d, Threshold: %f\n", weightInfo.accuracy * 100, weightInfo.correct + weightInfo.wrong, weightInfo.threshold);
		}
	} else if (choice == 3) {

		float difference[1001];
	    Weights weights[1001];
	    int i=0;
	    for (int gpg = 0; gpg <= 10; gpg++) {
	        for (int five_gpg = 0; five_gpg <= 10 - gpg; five_gpg++) {
	            for (int hgpg = 0; hgpg <= 10 - gpg - five_gpg; hgpg++) {
	                for (int tgpg = 0; tgpg <= 10 - gpg - five_gpg - hgpg; tgpg++) {

	                    float gpg_value = gpg / 10.0f;
	                    float five_gpg_value = five_gpg / 10.0f;
	                    float hgpg_value = hgpg / 10.0f;
	                    float tgpg_value = tgpg / 10.0f;
	                    float otga_value = 1.0f - gpg_value - five_gpg_value - hgpg_value - tgpg_value;

	                    set_weights(&weights[i], gpg_value, five_gpg_value, hgpg_value, tgpg_value, otga_value);
	                    difference[i] = calculate_probabilities_difference(players, num_players, &weights[i]);
	                    i+=1;
	                }
	            }
	        }
	    }

	    for (int i = 0; i < 1001; i++) {
	        printf("Weights: gpg=%.2f, five_gpg=%.2f, hgpg=%.2f, tgpg=%.2f, otga=%.2f, Difference: %.2f\n", weights[i].gpg, weights[i].five_gpg, weights[i].hgpg, weights[i].tgpg, weights[i].otga, difference[i]);
	    }

	    float min_difference = difference[0];
	    Weights min_weights = weights[0];
	    for (int i = 1; i < 1001; i++) {
            if (difference[i] < min_difference) {
                min_difference = difference[i];
                min_weights = weights[i];
            }
	    }
	    printf("Best weights: gpg=%.2f, five_gpg=%.2f, hgpg=%.2f, tgpg=%.2f, otga=%.2f\n", min_weights.gpg, min_weights.five_gpg, min_weights.hgpg, min_weights.tgpg, min_weights.otga);
	    printf("Difference: %.2f\n", min_difference);

	} else {
		printf("Invalid choice\n");
	}

}
