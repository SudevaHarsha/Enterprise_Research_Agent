"""Workers package: background execution entrypoints (task_013).

``app.workers.worker`` builds the Prefect 3 worker deployment (flow
``research-pipeline``, work pool ``research``, Postgres-backed queue) and
documents the compose worker command::

    prefect worker start --pool research
"""
