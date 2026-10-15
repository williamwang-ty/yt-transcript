"""Unified regression test entrypoint."""

from tests.regression_core import CoreRegressionTests
from tests.regression_language import LanguageRegressionTests
from tests.regression_runtime import RuntimeRegressionTests
from tests.regression_workflow import WorkflowRegressionTests

__all__ = [
    "CoreRegressionTests",
    "LanguageRegressionTests",
    "RuntimeRegressionTests",
    "WorkflowRegressionTests",
]
