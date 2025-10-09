use pyo3::Bound;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;
use pyo3::types::PyType;

// Take all data we have (list of player info objects and min/max values)
// Normalize all the stats
// Iterate over weights and calculate the probabilities + accuracy
// Save the top results


#[pyclass]
#[derive(Clone)]
pub struct PlayerInfo {
    #[pyo3(get, set)]
    pub gpg: f32,
    #[pyo3(get, set)]
    pub hgpg: f32,
    #[pyo3(get, set)]
    pub five_gpg: f32,
    #[pyo3(get, set)]
    pub tgpg: f32,
    #[pyo3(get, set)]
    pub otga: f32,
    #[pyo3(get, set)]
    pub hppg: f32,
    #[pyo3(get, set)]
    pub otshga: f32,
    #[pyo3(get, set)]
    pub is_home: f32,
    #[pyo3(get, set)]
    pub hppg_otshga: f32,
    #[pyo3(get, set)]
    pub scored: f32,
    #[pyo3(get, set)]
    pub tims: f32,
    #[pyo3(get, set)]
    pub date: String,
}

#[pymethods]
impl PlayerInfo {
    #[new]
    pub fn new(gpg: f32, hgpg: f32, five_gpg: f32, tgpg: f32, otga: f32, hppg: f32, 
               otshga: f32, is_home: f32, hppg_otshga: f32, scored: f32, tims: f32, date: String) -> Self {
        Self {
            gpg,
            hgpg,
            five_gpg,
            tgpg,
            otga,
            hppg,
            otshga,
            is_home,
            hppg_otshga,
            scored,
            tims,
            date,
        }
    }

    #[classmethod]
    fn default(_cls: &Bound<'_, PyType>) -> Self {
        Self {
            gpg: 0.0, hgpg: 0.0, five_gpg: 0.0, tgpg: 0.0,
            otga: 0.0, hppg: 0.0, otshga: 0.0, is_home: 0.0,
            hppg_otshga: 0.0, scored: 0.0, tims: 0.0, date: String::new(),
        }
    }
}

#[pyclass]
#[derive(Clone)]
pub struct MinMax {
    #[pyo3(get, set)]
    pub min_gpg: f32,
    #[pyo3(get, set)]
    pub max_gpg: f32,
    #[pyo3(get, set)]
    pub min_hgpg: f32,
    #[pyo3(get, set)]
    pub max_hgpg: f32,
    #[pyo3(get, set)]
    pub min_five_gpg: f32,
    #[pyo3(get, set)]
    pub max_five_gpg: f32,
    #[pyo3(get, set)]
    pub min_tgpg: f32,
    #[pyo3(get, set)]
    pub max_tgpg: f32,
    #[pyo3(get, set)]
    pub min_otga: f32,
    #[pyo3(get, set)]
    pub max_otga: f32,
    #[pyo3(get, set)]
    pub min_hppg: f32,
    #[pyo3(get, set)]
    pub max_hppg: f32,
    #[pyo3(get, set)]
    pub min_otshga: f32,
    #[pyo3(get, set)]
    pub max_otshga: f32,
}

#[pymethods]
impl MinMax {
    #[new]
    pub fn new(
        min_gpg: f32, max_gpg: f32,
        min_hgpg: f32, max_hgpg: f32,
        min_five_gpg: f32, max_five_gpg: f32,
        min_tgpg: f32, max_tgpg: f32,
        min_otga: f32, max_otga: f32,
        min_hppg: f32, max_hppg: f32,
        min_otshga: f32, max_otshga: f32,
    ) -> Self {
        Self {
            min_gpg, max_gpg,
            min_hgpg, max_hgpg,
            min_five_gpg, max_five_gpg,
            min_tgpg, max_tgpg,
            min_otga, max_otga,
            min_hppg, max_hppg,
            min_otshga, max_otshga,
        }
    }
    
    #[classmethod]
    fn default(_cls: &Bound<'_, PyType>) -> Self {
        Self {
            min_gpg: f32::MAX, max_gpg: f32::MIN,
            min_hgpg: f32::MAX, max_hgpg: f32::MIN,
            min_five_gpg: f32::MAX, max_five_gpg: f32::MIN,
            min_tgpg: f32::MAX, max_tgpg: f32::MIN,
            min_otga: f32::MAX, max_otga: f32::MIN,
            min_hppg: f32::MAX, max_hppg: f32::MIN,
            min_otshga: f32::MAX, max_otshga: f32::MIN,
        }
    }
}

#[pyclass]
#[derive(Clone, Copy)]
pub struct Weights {
    #[pyo3(get, set)]
    pub gpg: f32,
    #[pyo3(get, set)]
    pub hgpg: f32,
    #[pyo3(get, set)]
    pub five_gpg: f32,
    #[pyo3(get, set)]
    pub tgpg: f32,
    #[pyo3(get, set)]
    pub otga: f32,
    #[pyo3(get, set)]
    pub is_home: f32,
    #[pyo3(get, set)]
    pub hppg_otshga: f32,
}

#[pymethods]
impl Weights {
    #[new]
    pub fn new(gpg: f32, hgpg: f32, five_gpg: f32, tgpg: f32, otga: f32, 
               is_home: f32, hppg_otshga: f32) -> Self {
        Self {
            gpg,
            hgpg,
            five_gpg,
            tgpg,
            otga,
            is_home,
            hppg_otshga,
        }
    }

    fn sum(&self) -> f32 {
        self.gpg + self.hgpg + self.five_gpg + self.tgpg + self.otga + self.is_home + self.hppg_otshga
    }

    fn normalize(&mut self) {
        let sum = self.sum();
        if sum > 0.0 {
            self.gpg /= sum;
            self.hgpg /= sum;
            self.five_gpg /= sum;
            self.tgpg /= sum;
            self.otga /= sum;
            self.is_home /= sum;
            self.hppg_otshga /= sum;
        }
    }
}


// Normalize stats
fn normalize_stats(players: &mut [PlayerInfo], min_max: &MinMax) {
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
fn calculate_probabilities(players: &[PlayerInfo], probabilities: &mut [f32], weights: &Weights) {
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

// Calculate accuracy for a set of weights
fn calculate_accuracy(players: &[PlayerInfo], weights: &Weights) -> f32 {
    let mut probabilities = vec![0.0; players.len()];
    calculate_probabilities(players, &mut probabilities, weights);
    
    let mut correct_predictions = 0;
    let total_predictions = players.len();
    
    for (i, player) in players.iter().enumerate() {
        // Consider a prediction correct if probability > 0.5 and player scored, or probability <= 0.5 and player didn't score
        let predicted_to_score = probabilities[i] > 0.5;
        let actually_scored = player.scored > 0.5;
        
        if predicted_to_score == actually_scored {
            correct_predictions += 1;
        }
    }
    
    correct_predictions as f32 / total_predictions as f32
}

// Generate normalized weight combinations
fn generate_weight_combinations() -> Vec<Weights> {
    let mut combinations = Vec::new();
    let step = 0.1; // 10% increments
    
    // Generate all combinations where weights sum to approximately 1.0
    for gpg in (0..=10).map(|i| i as f32 * step) {
        for hgpg in (0..=10).map(|i| i as f32 * step) {
            for five_gpg in (0..=10).map(|i| i as f32 * step) {
                for tgpg in (0..=10).map(|i| i as f32 * step) {
                    for otga in (0..=10).map(|i| i as f32 * step) {
                        for is_home in (0..=10).map(|i| i as f32 * step) {
                            for hppg_otshga in (0..=10).map(|i| i as f32 * step) {
                                let mut weights = Weights::new(gpg, hgpg, five_gpg, tgpg, otga, is_home, hppg_otshga);
                                let sum = weights.sum();
                                
                                // Only consider combinations that sum to approximately 1.0 (+/- 0.05)
                                if sum >= 0.95 && sum <= 1.05 && sum > 0.0 {
                                    weights.normalize();
                                    combinations.push(weights);
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    
    combinations
}

#[pyclass]
#[derive(Clone)]
pub struct WeightResult {
    #[pyo3(get)]
    pub weights: Weights,
    #[pyo3(get)]
    pub accuracy: f32,
}

#[pymethods]
impl WeightResult {
    #[new]
    pub fn new(weights: Weights, accuracy: f32) -> Self {
        Self { weights, accuracy }
    }
}

// Test different weight combinations and return the best ones
#[pyfunction]
fn test_weights(py: Python, mut players: Vec<PlayerInfo>, min_max: MinMax, top_n: Option<usize>) -> PyResult<PyObject> {
    let top_n = top_n.unwrap_or(10);
    
    // Normalize the player stats
    normalize_stats(&mut players, &min_max);
    
    // Generate all weight combinations
    let weight_combinations = generate_weight_combinations();
    
    // Test each combination and collect results
    let mut results: Vec<WeightResult> = weight_combinations
        .iter()
        .map(|weights| {
            let accuracy = calculate_accuracy(&players, weights);
            WeightResult::new(*weights, accuracy)
        })
        .collect();
    
    // Sort by accuracy (highest first)
    results.sort_by(|a, b| b.accuracy.partial_cmp(&a.accuracy).unwrap());
    
    // Take top N results
    results.truncate(top_n);
    
    Ok(results.into_pyobject(py)?.into_any().unbind())
}

// Predict with specific weights
#[pyfunction]
fn predict_with_weights(py: Python, mut players: Vec<PlayerInfo>, min_max: MinMax, weights: Weights) -> PyResult<PyObject> {
    let mut probabilities = vec![0.0; players.len()];
    normalize_stats(&mut players, &min_max);
    calculate_probabilities(&players, &mut probabilities, &weights);

    Ok(probabilities.into_pyobject(py)?.into_any().unbind())
}

// Legacy predict function (for backward compatibility)
#[pyfunction]
fn predict(py: Python, players: Vec<PlayerInfo>, min_max: MinMax) -> PyResult<PyObject> {
    // Use equal weights as default
    let weights = Weights::new(1.0/7.0, 1.0/7.0, 1.0/7.0, 1.0/7.0, 1.0/7.0, 1.0/7.0, 1.0/7.0);
    predict_with_weights(py, players, min_max, weights)
}


// A Python module implemented in Rust.
#[pymodule]
fn test_weights_rust(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PlayerInfo>()?;
    m.add_class::<MinMax>()?;
    m.add_class::<Weights>()?;
    m.add_class::<WeightResult>()?;
    m.add_function(wrap_pyfunction!(predict, m)?)?;
    m.add_function(wrap_pyfunction!(predict_with_weights, m)?)?;
    m.add_function(wrap_pyfunction!(test_weights, m)?)?;
    Ok(())
}
