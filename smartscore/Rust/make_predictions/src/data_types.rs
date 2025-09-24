use pyo3::Bound;
use pyo3::prelude::*;
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
    #[pyo3(get, set)]
    pub scored: Option<f32>,
    #[pyo3(get, set)]
    pub tims: Option<i32>,
    #[pyo3(get, set)]
    pub date: Option<String>,
}

#[pymethods]
impl PlayerInfo {
    #[new]
    #[pyo3(signature = (
        gpg, hgpg, five_gpg, tgpg, otga, hppg, otshga, is_home, hppg_otshga,
        scored=None, tims=None, date=None
    ))]
    pub fn new(
        gpg: f32,
        hgpg: f32,
        five_gpg: f32,
        tgpg: f32,
        otga: f32,
        hppg: f32,
        otshga: f32,
        is_home: f32,
        hppg_otshga: f32,
        scored: Option<f32>,
        tims: Option<i32>,
        date: Option<String>
    ) -> Self {
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
            hppg_otshga: 0.0, scored: None, tims: None, date: None,
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
#[derive(Clone, Copy, Default, Debug)]
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

// Implement Display for Weights
use std::fmt;
impl fmt::Display for Weights {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Weights(gpg: {:.6}, five_gpg: {:.6}, hgpg: {:.6}, tgpg: {:.6}, otga: {:.6}, hppg_otshga: {:.6}, is_home: {:.6})",
            self.gpg, self.five_gpg, self.hgpg, self.tgpg, self.otga, self.hppg_otshga, self.is_home
        )
    }
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

    fn __repr__(&self) -> String {
        format!(
            "Weights(gpg: {:.6}, five_gpg: {:.6}, hgpg: {:.6}, tgpg: {:.6}, otga: {:.6}, hppg_otshga: {:.6}, is_home: {:.6})",
            self.gpg, self.five_gpg, self.hgpg, self.tgpg, self.otga, self.hppg_otshga, self.is_home
        )
    }

    fn __str__(&self) -> String {
        self.__repr__()
    }
}