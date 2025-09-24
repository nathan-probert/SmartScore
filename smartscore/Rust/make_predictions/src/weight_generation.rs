use pyo3::prelude::*;
use rayon::prelude::*;
use crossbeam::channel::unbounded;

/// Generate all possible weight combinations that sum to 1.0 using optimized mathematical approach
#[pyfunction]
pub fn generate_weight_permutations(_py: Python, step_size: f32) -> PyResult<Vec<crate::data_types::Weights>> {
    let step = step_size / 100.0;
    let target_steps = (1.0 / step) as i32;
    let max_value = target_steps;

    let (sender, receiver) = unbounded();

    (0..=max_value).into_par_iter().for_each(|w0| {
        for w1 in 0..=max_value {
            for w2 in 0..=max_value {
                for w3 in 0..=max_value {
                    for w4 in 0..=max_value {
                        for w5 in 0..=max_value {
                            let total = w0 + w1 + w2 + w3 + w4 + w5;
                            if total <= target_steps {
                                let w6 = target_steps - total;

                                let weights = crate::data_types::Weights {
                                    gpg: w0 as f32 * step,
                                    five_gpg: w1 as f32 * step,
                                    hgpg: w2 as f32 * step,
                                    tgpg: w3 as f32 * step,
                                    otga: w4 as f32 * step,
                                    hppg_otshga: w5 as f32 * step,
                                    is_home: w6 as f32 * step,
                                };
                                sender.send(weights).unwrap();
                            }
                        }
                    }
                }
            }
        }
    });

    drop(sender);
    let weight_combinations: Vec<_> = receiver.iter().collect();

    Ok(weight_combinations)
}