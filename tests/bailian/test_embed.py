"""Explicitly invoked, billable embedding example; no calls on import."""

from app.server.container import build_container
from app.server.core import Settings


def main():
    provider = build_container(Settings.from_env()).bailian
    vectors = provider.embed_texts(["衣服质量很好", "今天天气真好"])
    print({"count": len(vectors), "dimension": len(vectors[0].dense)})


if __name__ == "__main__":
    main()
