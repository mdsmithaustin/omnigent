from fastapi import FastAPI

from omnigent.harnesses.prime_native.bridge import executor_bridge_dir
from omnigent.inner.executor import Executor
from omnigent.inner.pi_native_executor import ExtensionNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_prime_native_executor() -> Executor:
    return ExtensionNativeExecutor(executor_bridge_dir(), agent_label="Prime Native")


def create_app() -> FastAPI:
    return ExecutorAdapter(executor_factory=_build_prime_native_executor).build()
