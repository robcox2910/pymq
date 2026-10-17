"""Check that every example imports cleanly, exposes ``main()`` and runs."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.py"))
KEY_LINES: dict[str, tuple[str, ...]] = {
    "broker_demo": ("dead letters: 1",),
    "work_queue": (
        "ann took job-0 (attempt 1) and crashed: her connection is lost",
        "the broker took job-0 back from ann (lease returned, attempt 1 used up)",
        "bob finished job-0 (attempt 2)",
    ),
    "topic_alerts": ("alerts queue holds: 2",),
}


def load_example(path: Path) -> ModuleType:
    """Import the example at ``path`` (examples/ is not a package)."""
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestExamples:
    """Test the runnable examples."""

    def test_examples_exist(self) -> None:
        """Find every example file."""
        assert {p.stem for p in EXAMPLE_FILES} == set(KEY_LINES)

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.stem)
    def test_exposes_main(self, path: Path) -> None:
        """Import each example and expect a callable ``main``."""
        assert callable(getattr(load_example(path), "main", None))

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.stem)
    def test_main_prints_key_line(self, path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Run each example to completion and see its key output lines, in order."""
        load_example(path).main()
        lines = capsys.readouterr().out.splitlines()
        positions = [lines.index(line) for line in KEY_LINES[path.stem]]
        assert positions == sorted(positions)
