"""Pipeline stage tasks (task_011): one Prefect @task per DAG stage."""

from app.pipeline.stages.collect import run_collect
from app.pipeline.stages.conclude import run_conclude
from app.pipeline.stages.define import run_define
from app.pipeline.stages.detect import run_detect
from app.pipeline.stages.extract import run_extract
from app.pipeline.stages.find import run_find
from app.pipeline.stages.search import run_search
from app.pipeline.stages.store import run_store
from app.pipeline.stages.trace import run_trace
from app.pipeline.stages.verify import run_verify

__all__ = [
    "run_collect",
    "run_conclude",
    "run_define",
    "run_detect",
    "run_extract",
    "run_find",
    "run_search",
    "run_store",
    "run_trace",
    "run_verify",
]
