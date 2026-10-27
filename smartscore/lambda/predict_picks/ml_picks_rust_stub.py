"""Pure-Python stand-in for the compiled `make_predictions_rust` extension.

The predict-picks image never runs the Rust portmanteau math, but its
import chain (ml_picks_handler -> utility.py -> constants.py) constructs
`WEIGHTS = make_predictions_rust.Weights(...)` at import time. The
Dockerfile copies this file into the image as `make_predictions_rust.py`
with the same shape as the stub in tests/unit/test_predict_picks.py; the
real Rust wheel (make compile) ships only to the zip-deployed Lambdas.
"""


class Weights:  # noqa: D101 (shape-compatible stub)
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
