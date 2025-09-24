use crate::data_types::{PlayerInfo, MinMax, Weights};
use pyo3::prelude::*;

// Normalize stats
pub fn normalize_stats(players: &mut [PlayerInfo], min_max: &MinMax) {
    fn normalize(value: f32, min: f32, max: f32) -> f32 {
        (value - min) / (max - min)
    }

    players.iter_mut().for_each(|player| {
        player.gpg = normalize(player.gpg, min_max.min_gpg, min_max.max_gpg);
        player.hgpg = normalize(player.hgpg, min_max.min_hgpg, min_max.max_hgpg);
        player.five_gpg = normalize(player.five_gpg, min_max.min_five_gpg, min_max.max_five_gpg);
        player.tgpg = normalize(player.tgpg, min_max.min_tgpg, min_max.max_tgpg);
        player.otga = normalize(player.otga, min_max.min_otga, min_max.max_otga);
        player.hppg = normalize(player.hppg, min_max.min_hppg, min_max.max_hppg);
        player.otshga = normalize(player.otshga, min_max.min_otshga, min_max.max_otshga);
        player.hppg_otshga = player.hppg * player.otshga;
    });
}

// Calculate probabilities
pub fn calculate_probabilities(players: &[PlayerInfo], probabilities: &mut [f32], weights: &Weights) {
    probabilities.iter_mut().zip(players.iter()).for_each(|(p, player)| {
        *p = player.gpg * weights.gpg
            + player.five_gpg * weights.five_gpg
            + player.hgpg * weights.hgpg
            + player.tgpg * weights.tgpg
            + player.otga * weights.otga
            + player.hppg_otshga * weights.hppg_otshga
            + player.is_home * weights.is_home;
    });
}

// Predict daily
#[pyfunction]
pub fn predict(py: Python, mut players: Vec<PlayerInfo>, min_max: MinMax, weights: Weights) -> PyResult<PyObject> {
    let mut probabilities = vec![0.0; players.len()];
    normalize_stats(&mut players, &min_max);
    calculate_probabilities(&players, &mut probabilities, &weights);

    Ok(probabilities.into_pyobject(py)?.into_any().unbind())
}