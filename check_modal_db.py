import modal
import os

app = modal.App("test-db-check")

@app.function(
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
)
def check_db():
    database_url = os.environ.get("DATABASE_URL", "NOT_SET")
    # Hide the password but show the host and DB name
    if "@" in database_url:
        masked = database_url.split("@")[-1]
    else:
        masked = database_url
    return {"database_url_masked": masked}

if __name__ == "__main__":
    with app.run():
        result = check_db.remote()
        print(result)
