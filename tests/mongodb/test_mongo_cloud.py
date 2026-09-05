"""Read-only Atlas connection check; credentials come from the root .env."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with MongoClient(os.environ["MONGODB_URI"], server_api=ServerApi("1"),
                     serverSelectionTimeoutMS=10000) as client:
        client.admin.command("ping")
        print("MongoDB ping succeeded")


if __name__ == "__main__":
    main()
