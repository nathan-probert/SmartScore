# Predict-only SmartScore ML picks Lambda (MLPicks-{ENV}).
# BUILT + PUSHED BY CI on every deploy (deploy job: `make build-ml-image` ->
# ECR PUBLIC master push (50 GB/month always-free tier) -> private-copy sync
# -> single deploy.sh stack update); `make build-ml-image` +
# `make deploy-ml-lambda` work for manual runs too.
# Training does NOT happen here.  docs/aws-predict-lambda-runbook.md
#
# BASE IMAGE: python:3.12-slim (NOT the heavier public.ecr.aws/lambda/python
# runtime base - measured at 190.6 MB compressed before a single python dep)
# plus the official pip Runtime Interface Client. A container Lambda
# REQUIRES the RIC, but it ships as a pip package (awslambdaric, ~1MB) -
# see the ENTRYPOINT below - so the slim base can carry it, which keeps the
# image far under the CI size sanity gate (DOCKER_IMAGE_MAX_MB, default
# 5120 MB; the ECR Public 50 GB/month tier is the real budget).
# python3.12 matches the repo runtime; images are pulled from the
# public.ecr.aws mirror of the Docker Hub library so CI never races
# Docker Hub rate limits.
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder

WORKDIR /build

# git is needed only to pip-install smartscore-info-client from GitHub;
# it never ships in the runtime stage.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# pandas/numpy/scipy/sklearn from PyPI wheels (which include the compiled
# deps), lightgbm 4.7.0 pinned to the training environment. supabase/postgrest
# + pytz are needed for the Supabase reads and ET-today clock; boto3 for the
# S3 artifact read; powertools for the handler's logger/metrics;
# smartscore-info-client is imported by smartscore/utility.py
# (exponential_backoff_request); awslambdaric is the Lambda runtime
# interface client (see base-image note above).
# --no-compile skips .pyc emission; --target keeps the tree free of
# pip/setuptools/venv scaffolding.
RUN pip install --no-cache-dir --no-compile \
      --target /opt/python \
      "pandas>=3.0.0,<4.0.0" \
      "numpy>=2.1,<3.0" \
      "scipy>=1.14,<2.0" \
      "scikit-learn>=1.9.1" \
      "lightgbm==4.7.0" \
      "boto3>=1.40.6" \
      "supabase>=2.27.0,<3.0.0" \
      "pytz>=2024.2" \
      "aws-lambda-powertools>=3.0.0" \
      "python-dotenv>=1.0.1" \
      "joblib>=1.4" \
      "awslambdaric>=2.0.0" \
      "smartscore-info-client @ git+https://github.com/nathan-probert/smartscore-info-client.git@v3.0.0"

# Runtime ballast prune (same convention as build_scripts/deploy.sh
# prune_output_dir): the scipy/scikit-learn wheels ship test-suites and
# benchmarks inside site-packages (~45-60MB) that a runtime-only image
# never executes; --no-compile already skips *.pyc. Pruning happens in the
# BUILDER stage: COPY --from=builder takes the final tree state, so
# deletions never land as extra layers in the image. *.dist-info is
# deliberately KEPT (a few KB total): importlib.metadata version lookups
# (aws-lambda-powertools etc.) need it.
RUN find /opt/python -depth -type d \( -name tests -o -name test -o -name benchmarks -o -name "__pycache__" \) -exec rm -rf {} + \
  && find /opt/python -type d -name "*.egg-info" -exec rm -rf {} + \
  && find /opt/python -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete \
  && rm -rf /opt/python/bin

FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /var/task

# Runtime python deps live beside the task code on PYTHONPATH.
COPY --from=builder /opt/python /var/python

# The predict-only image never runs the Rust portmanteau math, but the
# import chain ml_picks_handler -> utility.py -> constants.py constructs
# `make_predictions_rust.Weights(...)` at import time. Ship the pure-Python
# stub (same shape as tests/unit/test_predict_picks.py) instead of the
# compiled .so the zip-deployed Lambdas carry.
COPY smartscore/lambda/predict_picks/ml_picks_rust_stub.py make_predictions_rust.py
COPY smartscore/lambda/predict_picks/handler.py ml_picks_handler.py
COPY smartscore/lambda/predict_picks/__init__.py ml_picks/__init__.py
COPY smartscore/config.py config.py
COPY smartscore/constants.py constants.py
COPY smartscore/utility.py utility.py
COPY smartscore/scripts/backtrack/model.py frame/model.py
COPY smartscore/scripts/backtrack/lineup_project.py frame/lineup_project.py

# Runway: the flat module layout mimics the deploy.sh zip convention (modules
# beside each other); `frame/model.py` is on sys.path only for the model
# import, never for smartscore's own modules.
ENV PYTHONPATH=/var/task:/var/python

# RIC entry: a slim image has no lambda wrapper, so invoke the pip-installed
# Runtime Interface Client directly (equivalent to the AWS base image's
# ENTRYPOINT + CMD pair). Lambda requires the image to define ENTRYPOINT;
# CMD supplies the handler spec.
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ml_picks_handler.lambda_handler"]
