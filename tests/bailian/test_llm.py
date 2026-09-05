"""Explicitly invoked, billable chat example; no calls on import."""

from app.server.container import build_container
from app.server.core import Settings


def main():
    provider = build_container(Settings.from_env()).bailian
    answer = provider.chat([{"role": "user", "content": "hello"}])
    print(answer.content)


if __name__ == "__main__":
    main()
