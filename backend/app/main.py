from fastapi import FastAPI, status
from dotenv import load_dotenv
from pathlib import Path

# Initialize the application
app = FastAPI()
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Create an endpoint that ensures it is up and running
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}