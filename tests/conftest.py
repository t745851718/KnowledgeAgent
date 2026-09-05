"""SDK examples are manually invoked scripts, not automatic pytest cases."""

collect_ignore_glob = [
    "bailian/*.py", "mineru/*.py", "mongodb/*.py", "minio/*.py", "zilliz/*.py",
]
