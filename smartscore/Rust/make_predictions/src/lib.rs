use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

mod data_types;
mod predictions;
mod weight_testing;
mod weight_generation;

// Re-export the data types for Python
pub use data_types::{PlayerInfo, MinMax, Weights};

// Re-export the functions for Python
pub use predictions::predict;
pub use weight_testing::test_weights;
pub use weight_generation::generate_weight_permutations;

// A Python module implemented in Rust.
#[pymodule]
fn make_predictions_rust(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PlayerInfo>()?;
    m.add_class::<MinMax>()?;
    m.add_class::<Weights>()?;
    m.add_class::<weight_generation::WeightGenerator>()?;
    m.add_function(wrap_pyfunction!(predict, m)?)?;
    m.add_function(wrap_pyfunction!(test_weights, m)?)?;
    m.add_function(wrap_pyfunction!(generate_weight_permutations, m)?)?;
    Ok(())
}
