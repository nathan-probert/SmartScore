import base64
import csv
import gzip
import json
import os

import boto3
import pandas as pd

PATH = "smartscore\\lib"
DATA_PATH = f"{PATH}\\data.csv"

# FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga"]
FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga", "hppg", "otshga", "home"]


def invoke_lambda(function_name, payload, wait=True):
    sts_client = boto3.client("sts")
    lambda_client = boto3.client("lambda")

    session = boto3.session.Session()
    region = session.region_name
    account_id = sts_client.get_caller_identity()["Account"]
    invocation_type = "RequestResponse" if wait else "Event"

    function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
    response = lambda_client.invoke(
        FunctionName=function_arn, InvocationType=invocation_type, Payload=json.dumps(payload)
    )
    response_payload = json.loads(response["Payload"].read())
    return response_payload


def unpack_response(body):
    compressed_data = base64.b64decode(body)
    decompressed_data = gzip.decompress(compressed_data).decode("utf-8")
    original_data = json.loads(decompressed_data)

    return original_data


def create_csv():
    response = invoke_lambda("Api-prod", {"method": "GET_ALL"})
    data = unpack_response(response.get("entries"))

    # Get the fields from the last entry (which should have all fields), set missing fields to None
    all_fields = data[-1].keys()
    for entry in data:
        for field in all_fields:
            entry.setdefault(field, None)

    os.makedirs(PATH, exist_ok=True)
    with open(DATA_PATH, "w+", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields)
        writer.writeheader()
        writer.writerows(data)


def get_data():
    # Uncomment this to ask about downloading data each time
    # print("Do you want to download the data from the database? (y/n)")
    # choice = input().split()[0].lower()
    # if choice == "y":
    #     create_csv()

    data = pd.read_csv(DATA_PATH, encoding="utf-8", low_memory=False)

    # Clean the data
    for col in [col for col in data.columns if col not in ["date", "name"]]:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=FEATURES + ["scored"])
    labels = data["scored"].astype(int)

    # Display info about the data
    print()
    print(labels.value_counts())
    print(f"Ratio of goal scorers: {labels.mean():.2f}\n")

    return data, labels
