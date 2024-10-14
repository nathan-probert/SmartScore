import traceback

from aws_lambda_powertools import Logger

logger = Logger()


def lambda_handler_error_responder(func):
    def wrapper(event, context):
        try:
            return func(event, context)
        except Exception as exc:
            tb_str = traceback.format_exc()
            logger.error(f"Error occurred: {str(exc)}\nTraceback:\n{tb_str}")

            raise exc

    return wrapper
