from fastapi import FastAPI, status
from .api import plaid

# Initialize the application (settings and .env are loaded in app/config.py)
app = FastAPI()
app.include_router(plaid.router)

# Create an endpoint that ensures it is up and running
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}
