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


#[pyclass]
pub struct WeightGenerator {
    step: f32,
    n: i32,

    // loop state
    w0: i32,
    w1: i32,
    w2: i32,
    w3: i32,
    w4: i32,
    w5: i32,

    done: bool,
}

#[pymethods]
impl WeightGenerator {
    #[new]
    pub fn new(step_size: f32) -> Self {
        let step = step_size / 100.0;
        let n = (1.0 / step) as i32;

        Self {
            step,
            n,
            w0: 0,
            w1: 0,
            w2: 0,
            w3: 0,
            w4: 0,
            w5: 0,
            done: false,
        }
    }

    /// Generate up to `max_combos` weight combinations
    /// Returns a flat Vec<f32> (length = 7 * combos)
    pub fn next_chunk(&mut self, max_combos: usize) -> Vec<f32> {
        if self.done {
            return Vec::new();
        }

        let mut out = Vec::with_capacity(max_combos * 7);
        let mut produced = 0;

        let n = self.n;
        let step = self.step;

        while produced < max_combos {
            let r0 = n - self.w0;
            let r1 = r0 - self.w1;
            let r2 = r1 - self.w2;
            let r3 = r2 - self.w3;
            let r4 = r3 - self.w4;

            if self.w5 <= r4 {
                let w6 = r4 - self.w5;

                out.extend_from_slice(&[
                    self.w0 as f32 * step,
                    self.w1 as f32 * step,
                    self.w2 as f32 * step,
                    self.w3 as f32 * step,
                    self.w4 as f32 * step,
                    self.w5 as f32 * step,
                    w6 as f32 * step,
                ]);

                produced += 1;
                self.w5 += 1;
                continue;
            }

            // carry logic (manual nested-loop unwinding)
            self.w5 = 0;
            self.w4 += 1;
            if self.w4 <= r3 {
                continue;
            }

            self.w4 = 0;
            self.w3 += 1;
            if self.w3 <= r2 {
                continue;
            }

            self.w3 = 0;
            self.w2 += 1;
            if self.w2 <= r1 {
                continue;
            }

            self.w2 = 0;
            self.w1 += 1;
            if self.w1 <= r0 {
                continue;
            }

            self.w1 = 0;
            self.w0 += 1;
            if self.w0 <= n {
                continue;
            }

            self.done = true;
            break;
        }

        out
    }
}
