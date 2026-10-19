"""Unified regression test entrypoint."""

from tests.regression_agent_cli import AgentCliRegressionTests
from tests.regression_chunking import ChunkingTests, PostMergeCleanupTests
from tests.regression_core import CoreRegressionTests
from tests.regression_e2e import EndToEndFixtureTests
from tests.regression_language import LanguageRegressionTests
from tests.regression_run import RunCommandTests
from tests.regression_quality import AtomicWriteTests, GlossaryTests, VerifyQualityTests
from tests.regression_window import (
    WindowBilingualTests,
    WindowCueSplitTests,
    WindowExecutorTests,
    WindowOutlineTests,
    WindowProtocolTests,
    WindowSizingTests,
)
from tests.regression_workflow import WorkflowRegressionTests

__all__ = [
    "AgentCliRegressionTests",
    "AtomicWriteTests",
    "ChunkingTests",
    "CoreRegressionTests",
    "EndToEndFixtureTests",
    "GlossaryTests",
    "LanguageRegressionTests",
    "PostMergeCleanupTests",
    "RunCommandTests",
    "VerifyQualityTests",
    "WindowBilingualTests",
    "WindowCueSplitTests",
    "WindowExecutorTests",
    "WindowOutlineTests",
    "WindowProtocolTests",
    "WindowSizingTests",
    "WorkflowRegressionTests",
]
