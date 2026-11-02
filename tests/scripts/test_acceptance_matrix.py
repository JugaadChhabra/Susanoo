import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "acceptance_matrix", Path(__file__).parents[2] / "scripts/acceptance_matrix.py")
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def test_matrix_has_exactly_the_full_build_requirements():
    assert set(matrix.REQUIREMENTS) == {f"T{number:02}" for number in range(1, 41)}
    assert all(matrix.REQUIREMENTS.values())


def test_missing_or_skipped_tests_cannot_pass(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text('<testsuite><testcase classname="tests.runtime.test_runner_local" '
                    'name="test_m2_false_completion_fails_from_independent_evidence"><skipped/>'
                    '</testcase></testsuite>', encoding="utf-8")
    result = matrix.evaluate_matrix([path])
    assert result["T02"]["status"] == "not_accepted"
    assert result["T01"]["missing_selectors"]
    path.write_text('<testsuite><testcase classname="tests.runtime.test_runner_local" '
                    'name="test_m2_false_completion_fails_from_independent_evidence"/></testsuite>', encoding="utf-8")
    assert matrix.evaluate_matrix([path])["T02"]["status"] == "passed"
