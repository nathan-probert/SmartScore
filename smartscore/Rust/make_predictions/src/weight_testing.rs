use crate::data_types::{PlayerInfo, Weights};
use crate::predictions::{normalize_stats, calculate_probabilities};
use pyo3::prelude::*;

#[pyfunction]
pub fn test_weights(_py: Python, mut players: Vec<PlayerInfo>, min_max: crate::data_types::MinMax, weight_combinations: Vec<Weights>) -> PyResult<(Weights, i32, i32)> {
    let mut max_correct = -1;
    let mut best_weights = <Weights as Default>::default();
    let mut best_total = 0;
    let _total_combinations = weight_combinations.len();
    let _last_progress = 0;

    normalize_stats(&mut players, &min_max);

    for (_i, weights) in weight_combinations.iter().enumerate() {
        let mut probabilities = vec![0.0; players.len()];
        calculate_probabilities(&players, &mut probabilities, &weights);

        let (correct, total) = evaluate_correctness_with_total(&players, &probabilities);

        if correct >= max_correct {
            max_correct = correct;
            best_total = total;
            best_weights = *weights;
        }

        // Print progress every 1%
        // let current_progress = ((i + 1) as f64 / total_combinations as f64 * 100.0) as i32;
        // if current_progress > last_progress {
        //     println!("Progress: {}% ({}/{})", current_progress, i + 1, total_combinations);
        //     last_progress = current_progress;
        // }
    }

    Ok((best_weights, max_correct, best_total))
}


// Helper function to evaluate correctness and return both correct and total predictions
pub fn evaluate_correctness_with_total(players: &[PlayerInfo], probabilities: &[f32]) -> (i32, i32) {
    let mut correct = 0;
    let mut total_predictions = 0;
    let mut current_date_players = Vec::new();
    let mut last_date = players[0].date.clone();

    for (i, player) in players.iter().enumerate() {
        if last_date != player.date {
            // Process previous date - select 1 player from each TIMS group
            let date_correct = process_date_predictions(&current_date_players, players, probabilities);
            correct += date_correct;
            total_predictions += 3;

            // Reset for next date
            current_date_players.clear();
            last_date = player.date.clone();
        }

        // Only consider TIMS picks for predictions
        if let Some(tims) = player.tims {
            if tims >= 1 && tims <= 3 {
                current_date_players.push(i);
            }
        }
    }

    // Process the last date
    let date_correct = process_date_predictions(&current_date_players, players, probabilities);
    correct += date_correct;
    total_predictions += 3;

    (correct, total_predictions)
}

// Helper function to process predictions for a single date
pub fn process_date_predictions(player_indices: &[usize], players: &[PlayerInfo], probabilities: &[f32]) -> i32 {
    let mut correct = 0;
    let mut selected_players = [None; 3]; // One for each TIMS group (1, 2, 3)

    // Find the best player for each TIMS group
    for &player_idx in player_indices.iter() {
        let player = &players[player_idx];
        let prob = probabilities[player_idx];
        if let Some(tims) = player.tims {
            let group_idx = (tims - 1) as usize;

            if group_idx < 3 {
                match &selected_players[group_idx] {
                    None => selected_players[group_idx] = Some((player_idx, prob)),
                    Some((_, current_prob)) if prob > *current_prob => {
                        selected_players[group_idx] = Some((player_idx, prob));
                    }
                    _ => {}
                }
            }
        }
    }

    // Count correct predictions and total predictions made
    for selected in selected_players.iter() {
        if let Some((player_idx, _)) = selected {
            if let Some(scored) = players[*player_idx].scored {
                if scored > 0.0 {
                    correct += 1;
                }
            }
        }
    }

    correct
}