# Predict-only SmartScore ML picks Lambda (MLPicks-{ENV}).
# Built/deployed via `make build-ml-image` + `make deploy-ml-lambda`
# (docs/aws-predict-lambda-runbook.md). python:3.12-slim matches the repo's
# python3.12 Lambda runtime; training does NOT happen here.
FROM python:3.12-slim

WORKDIR /var/task

# pandas/numpy/scipy/sklearn from PyPI wheels (which include the compiled
# deps), lightgbm 4.7.0 pinned to the training environment. supabase/postgrest
# + pytz are needed for the Supabase reads and ET-today clock; boto3 ships in
# the base image only for boto3.client("s3") we still install explicitly.
RUN pip install --no-cache-dir \
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
    "joblib>=1.4"

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
ENV PYTHONPATH=/var/task

CMD ["ml_picks_handler.lambda_handler"]
