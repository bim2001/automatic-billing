
from pathlib import Path
from dotenv import dotenv_values
import boto3

env = dotenv_values(Path(__file__).resolve().parent / ".env")

bucket = env.get("R2_BUCKET_NAME")
access_key = env.get("R2_ACCESS_KEY_ID")
secret_key = env.get("R2_SECRET_ACCESS_KEY")
endpoint = env.get("R2_ENDPOINT_URL")

if not all([bucket, access_key, secret_key, endpoint]):
    raise SystemExit("STOP: May kulang na R2 setting.")

if bucket != "apartment-tenant-media":
    raise SystemExit("STOP: Hindi tugma ang bucket.")

if not endpoint.startswith("https://") or ".r2.cloudflarestorage.com" not in endpoint:
    raise SystemExit("STOP: Hindi inaasahan ang endpoint.")

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name="auto",
)

for prefix in ("ids/", "selfies/"):
    print(f"\n--- {prefix} ---")
    count = 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix:
                continue

            count += 1
            print(f"File {count}: {key.rsplit('/', 1)[-1]}")

    print(f"Total files: {count}")

print("\nREAD-ONLY CHECK COMPLETE")
