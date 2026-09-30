"""Shared test configuration.

Intent: unit tests must be isolated from real external services (Supabase and
AWS). For that reason the mocking (Supabase client, boto3 + dummy AWS creds)
lives in ``tests/unit/conftest.py`` so it only applies to unit tests, never to
the AWS-dev integration tests (which need the real boto3 client and the real
AWS credentials provided by the CI environment / local AWS profile).
"""
