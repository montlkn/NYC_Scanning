import modal
import os

app = modal.App("test-env-check")

@app.function(
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
)
def check_env():
    return {
        "USE_SCAN_V2": os.environ.get("USE_SCAN_V2", "NOT_SET"),
        "DATABASE_URL_SET": "DATABASE_URL" in os.environ,
        "FOOTPRINTS_DB_URL_SET": "FOOTPRINTS_DB_URL" in os.environ
    }

if __name__ == "__main__":
    with app.run():
        result = check_env.remote()
        print(result)
