import modal
import os

app = modal.App("test-db-check-v2")

@app.function(
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
)
def check_db():
    f_url = os.environ.get("FOOTPRINTS_DB_URL", "NOT_SET")
    if "@" in f_url:
        masked = f_url.split("@")[-1]
    else:
        masked = f_url
    return {"footprints_db_url_masked": masked}

if __name__ == "__main__":
    with app.run():
        result = check_db.remote()
        print(result)
