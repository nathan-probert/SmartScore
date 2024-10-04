import json

from aws_lambda_powertools import Logger

logger = Logger()


def lambda_handler_error_responder(func):
    def wrapper(event, context):
        try:
            return func(event, context)
        except Exception as exc:  # noqa F821
            logger.error(f"Error occurred: {str(exc)}")

            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Internal Server Error", "message": str(exc)}),
            }

    return wrapper
