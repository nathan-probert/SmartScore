use pyo3::Bound;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;
use pyo3::types::PyType;


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
}

#[pymethods]
impl PlayerInfo {
    #[new]
    pub fn new(gpg: f32, hgpg: f32, five_gpg: f32, tgpg: f32, otga: f32, hppg: f32, 
               otshga: f32, is_home: f32, hppg_otshga: f32) -> Self {
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
        }
    }

    #[classmethod]
    fn default(_cls: &Bound<'_, PyType>) -> Self {
        Self {
            gpg: 0.0, hgpg: 0.0, five_gpg: 0.0, tgpg: 0.0,
            otga: 0.0, hppg: 0.0, otshga: 0.0, is_home: 0.0,
            hppg_otshga: 0.0,
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
    
    #[classmethod]
    fn default(_cls: &Bound<'_, PyType>) -> Self {
        Self {
            gpg: 1.0,
            hgpg: 1.0,
            five_gpg: 1.0,
            tgpg: 1.0,
            otga: 1.0,
            is_home: 1.0,
            hppg_otshga: 1.0,
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

// Predict daily
#[pyfunction]
fn predict(py: Python, mut players: Vec<PlayerInfo>, min_max: MinMax, weights: Weights) -> PyResult<PyObject> {
    let mut probabilities = vec![0.0; players.len()];
    normalize_stats(&mut players, &min_max);
    calculate_probabilities(&players, &mut probabilities, &weights);

    Ok(probabilities.into_pyobject(py)?.into_any().unbind())
}


// A Python module implemented in Rust.
#[pymodule]
fn make_predictions_rust(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PlayerInfo>()?;
    m.add_class::<MinMax>()?;
    m.add_class::<Weights>()?;
    m.add_function(wrap_pyfunction!(predict, m)?)?;
    Ok(())
}
