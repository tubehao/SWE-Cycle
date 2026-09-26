#!/usr/bin/env python3
"""
Zero-dependency swebench grading script.

Inlines the minimal subset of swebench grading logic so that it can run inside
Docker containers without downloading any packages at runtime.

Replaces the previous `uv run parser.py` approach that required downloading
swebench + datasets + transitive deps on every container boot.

Source functions are taken from:
  - swebench.harness.constants (TestStatus, EvalType, string markers, FAIL_ONLY_REPOS)
  - swebench.harness.log_parsers.python (all Python-repo log parsers)
  - swebench.harness.grading (test_passed, test_failed, get_logs_eval, etc.)
"""

import json
import os
import re
import sys
from enum import Enum


# ============================================================================
# Constants (from swebench.harness.constants)
# ============================================================================

FAIL_TO_PASS = "FAIL_TO_PASS"
PASS_TO_PASS = "PASS_TO_PASS"
FAIL_TO_FAIL = "FAIL_TO_FAIL"
PASS_TO_FAIL = "PASS_TO_FAIL"

KEY_INSTANCE_ID = "instance_id"

APPLY_PATCH_FAIL = ">>>>> Patch Apply Failed"
RESET_FAILED = ">>>>> Reset Failed"
TESTS_ERROR = ">>>>> Tests Errored"
TESTS_TIMEOUT = ">>>>> Tests Timed Out"
START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"

FAIL_ONLY_REPOS = {
    "chartjs/Chart.js",
    "processing/p5.js",
    "markedjs/marked",
}


class TestStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


class EvalType(Enum):
    PASS_AND_FAIL = "pass_and_fail"
    FAIL_ONLY = "fail_only"


# ============================================================================
# Log Parsers (from swebench.harness.log_parsers.python)
# ============================================================================

def parse_log_pytest(log):
    test_status_map = {}
    for line in log.split("\n"):
        if any(line.startswith(x.value) for x in TestStatus):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) <= 1:
                continue
            test_status_map[test_case[1]] = test_case[0]
    return test_status_map


def parse_log_pytest_options(log):
    option_pattern = re.compile(r"(.*?)\[(.*)\]")
    test_status_map = {}
    for line in log.split("\n"):
        if any(line.startswith(x.value) for x in TestStatus):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) <= 1:
                continue
            has_option = option_pattern.search(test_case[1])
            if has_option:
                main, option = has_option.groups()
                if (
                    option.startswith("/")
                    and not option.startswith("//")
                    and "*" not in option
                ):
                    option = "/" + option.split("/")[-1]
                test_name = f"{main}[{option}]"
            else:
                test_name = test_case[1]
            test_status_map[test_name] = test_case[0]
    return test_status_map


def parse_log_django(log):
    test_status_map = {}
    lines = log.split("\n")

    prev_test = None
    for line in lines:
        line = line.strip()

        if "--version is equivalent to version" in line:
            test_status_map["--version is equivalent to version"] = (
                TestStatus.PASSED.value
            )

        if " ... " in line:
            prev_test = line.split(" ... ")[0]

        pass_suffixes = (" ... ok", " ... OK", " ...  OK")
        for suffix in pass_suffixes:
            if line.endswith(suffix):
                if line.strip().startswith(
                    "Applying sites.0002_alter_domain_unique...test_no_migrations"
                ):
                    line = line.split("...", 1)[-1].strip()
                test = line.rsplit(suffix, 1)[0]
                test_status_map[test] = TestStatus.PASSED.value
                break
        if " ... skipped" in line:
            test = line.split(" ... skipped")[0]
            test_status_map[test] = TestStatus.SKIPPED.value
        if line.endswith(" ... FAIL"):
            test = line.split(" ... FAIL")[0]
            test_status_map[test] = TestStatus.FAILED.value
        if line.startswith("FAIL:"):
            test = line.split()[1].strip()
            test_status_map[test] = TestStatus.FAILED.value
        if line.endswith(" ... ERROR"):
            test = line.split(" ... ERROR")[0]
            test_status_map[test] = TestStatus.ERROR.value
        if line.startswith("ERROR:"):
            test = line.split()[1].strip()
            test_status_map[test] = TestStatus.ERROR.value

        if line.lstrip().startswith("ok") and prev_test is not None:
            test = prev_test
            test_status_map[test] = TestStatus.PASSED.value

    patterns = [
        r"^(.*?)\s\.\.\.\sTesting\ against\ Django\ installed\ in\ ((?s:.*?))\ silenced\)\.\nok$",
        r"^(.*?)\s\.\.\.\sInternal\ Server\ Error:\ \/(.*)\/\nok$",
        r"^(.*?)\s\.\.\.\sSystem check identified no issues \(0 silenced\)\nok$",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, log, re.MULTILINE):
            test_name = match.group(1)
            test_status_map[test_name] = TestStatus.PASSED.value
    return test_status_map


def parse_log_pytest_v2(log):
    test_status_map = {}

    ansi_escape = re.compile(
        r'\x1b\[[0-9;]*[mKGHfABCDsuJSTl]|\x1b\][^\x07]*\x07|\x1b[=>]|\[\d+m'
    )

    for line in log.split("\n"):
        line = ansi_escape.sub('', line)
        escapes = "".join([chr(char) for char in range(1, 32)])
        translator = str.maketrans("", "", escapes)
        line = line.translate(translator).strip()

        if not line:
            continue

        if any(line.startswith(x.value) for x in TestStatus):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) >= 2:
                test_status_map[test_case[1]] = test_case[0]
        elif any(line.endswith(x.value) for x in TestStatus):
            test_case = line.split()
            if len(test_case) >= 2:
                test_status_map[test_case[0]] = test_case[1]
        else:
            for status in TestStatus:
                if re.search(rf'\b{re.escape(status.value)}\b', line):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            status_idx = parts.index(status.value)
                            if status_idx > 0:
                                test_status_map[parts[0]] = status.value
                                break
                        except ValueError:
                            continue

    return test_status_map


def parse_log_seaborn(log):
    test_status_map = {}
    for line in log.split("\n"):
        if line.startswith(TestStatus.FAILED.value):
            test_case = line.split()[1]
            test_status_map[test_case] = TestStatus.FAILED.value
        elif f" {TestStatus.PASSED.value} " in line:
            parts = line.split()
            if parts[1] == TestStatus.PASSED.value:
                test_case = parts[0]
                test_status_map[test_case] = TestStatus.PASSED.value
        elif line.startswith(TestStatus.PASSED.value):
            parts = line.split()
            test_case = parts[1]
            test_status_map[test_case] = TestStatus.PASSED.value
    return test_status_map


def parse_log_sympy(log):
    test_status_map = {}
    pattern = r"(_*) (.*)\.py:(.*) (_*)"
    matches = re.findall(pattern, log)
    for match in matches:
        test_case = f"{match[1]}.py:{match[2]}"
        test_status_map[test_case] = TestStatus.FAILED.value
    for line in log.split("\n"):
        line = line.strip()
        if line.startswith("test_"):
            if line.endswith(" E"):
                test = line.split()[0]
                test_status_map[test] = TestStatus.ERROR.value
            if line.endswith(" F"):
                test = line.split()[0]
                test_status_map[test] = TestStatus.FAILED.value
            if line.endswith(" ok"):
                test = line.split()[0]
                test_status_map[test] = TestStatus.PASSED.value
    return test_status_map


def parse_log_matplotlib(log):
    test_status_map = {}
    for line in log.split("\n"):
        line = line.replace("MouseButton.LEFT", "1")
        line = line.replace("MouseButton.RIGHT", "3")
        if any(line.startswith(x.value) for x in TestStatus):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) <= 1:
                continue
            test_status_map[test_case[1]] = test_case[0]
    return test_status_map


MAP_REPO_TO_PARSER = {
    "astropy/astropy": parse_log_pytest_v2,
    "django/django": parse_log_django,
    "marshmallow-code/marshmallow": parse_log_pytest,
    "matplotlib/matplotlib": parse_log_matplotlib,
    "mwaskom/seaborn": parse_log_seaborn,
    "pallets/flask": parse_log_pytest,
    "psf/requests": parse_log_pytest_options,
    "pvlib/pvlib-python": parse_log_pytest,
    "pydata/xarray": parse_log_pytest,
    "pydicom/pydicom": parse_log_pytest_options,
    "pylint-dev/astroid": parse_log_pytest,
    "pylint-dev/pylint": parse_log_pytest_options,
    "pytest-dev/pytest": parse_log_pytest,
    "pyvista/pyvista": parse_log_pytest,
    "scikit-learn/scikit-learn": parse_log_pytest_v2,
    "sqlfluff/sqlfluff": parse_log_pytest,
    "sphinx-doc/sphinx": parse_log_pytest_v2,
    "sympy/sympy": parse_log_sympy,
}


# ============================================================================
# Grading logic (from swebench.harness.grading)
# ============================================================================

def test_passed(case, sm):
    return case in sm and sm[case] in [TestStatus.PASSED.value, TestStatus.XFAIL.value]


def test_failed(case, sm):
    return case not in sm or sm[case] in [
        TestStatus.FAILED.value,
        TestStatus.ERROR.value,
    ]


def get_logs_eval(repo, log_fp):
    log_parser = MAP_REPO_TO_PARSER[repo]

    with open(log_fp) as f:
        content = f.read()
        bad_codes = [
            x for x in [APPLY_PATCH_FAIL, RESET_FAILED, TESTS_ERROR, TESTS_TIMEOUT]
            if x in content
        ]
        if bad_codes:
            return {}, False
        if not (START_TEST_OUTPUT in content and END_TEST_OUTPUT in content):
            return {}, False

        test_content = content.split(START_TEST_OUTPUT)[1].split(END_TEST_OUTPUT)[0]
        status_map = log_parser(test_content)

        if not status_map:
            status_map = log_parser(content)

        return status_map, True


def get_eval_tests_report(
    eval_status_map,
    gold_results,
    eval_type=EvalType.PASS_AND_FAIL,
):
    def check_pass_and_fail(test_case, eval_sm, success, failed):
        if test_passed(test_case, eval_sm):
            success.append(test_case)
        elif test_failed(test_case, eval_sm):
            failed.append(test_case)

    def check_fail_only(test_case, eval_sm, success, failed):
        if (
            test_case in eval_sm
            and eval_sm[test_case] == TestStatus.FAILED.value
        ):
            failed.append(test_case)
        else:
            success.append(test_case)

    check_test_case = (
        check_pass_and_fail if eval_type == EvalType.PASS_AND_FAIL else check_fail_only
    )

    f2p_success = []
    f2p_failure = []
    for test_case in gold_results[FAIL_TO_PASS]:
        check_test_case(test_case, eval_status_map, f2p_success, f2p_failure)

    p2p_success = []
    p2p_failure = []
    for test_case in gold_results[PASS_TO_PASS]:
        check_test_case(test_case, eval_status_map, p2p_success, p2p_failure)

    return {
        FAIL_TO_PASS: {"success": f2p_success, "failure": f2p_failure},
        PASS_TO_PASS: {"success": p2p_success, "failure": p2p_failure},
    }


def compute_fail_to_pass(report: dict) -> float:
    total = len(report[FAIL_TO_PASS]["success"]) + len(report[FAIL_TO_PASS]["failure"])
    if total == 0:
        return 1.0
    return len(report[FAIL_TO_PASS]["success"]) / total


def compute_pass_to_pass(report: dict) -> float:
    total = len(report[PASS_TO_PASS]["success"]) + len(report[PASS_TO_PASS]["failure"])
    if total == 0:
        return 1.0
    return len(report[PASS_TO_PASS]["success"]) / total


# ============================================================================
# Main entry point
# ============================================================================

def main():
    with open("/tests/config.json") as f:
        datum = json.load(f)

    repo = datum["repo"]
    instance_id = datum[KEY_INSTANCE_ID]

    f2p_raw = datum.get("FAIL_TO_PASS") or datum.get("fail_to_pass") or "[]"
    p2p_raw = datum.get("PASS_TO_PASS") or datum.get("pass_to_pass") or "[]"
    f2p_tests = json.loads(f2p_raw) if isinstance(f2p_raw, str) else f2p_raw
    p2p_tests = json.loads(p2p_raw) if isinstance(p2p_raw, str) else p2p_raw

    report_map = {
        instance_id: {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": False,
            "resolved": False,
        }
    }

    test_log_path = os.environ.get("LOG_FILE", "/logs/verifier/test_output.log")

    eval_status_map, found = get_logs_eval(repo, test_log_path)
    if found:
        report_map[instance_id]["patch_successfully_applied"] = True

        eval_ref = {
            FAIL_TO_PASS: f2p_tests,
            PASS_TO_PASS: p2p_tests,
        }

        eval_type = (
            EvalType.FAIL_ONLY if repo in FAIL_ONLY_REPOS else EvalType.PASS_AND_FAIL
        )

        report = get_eval_tests_report(eval_status_map, eval_ref, eval_type=eval_type)
        f2p_score = compute_fail_to_pass(report)
        p2p_score = compute_pass_to_pass(report)
        report_map[instance_id]["f2p_score"] = f2p_score
        report_map[instance_id]["p2p_score"] = p2p_score
        if f2p_score == 1.0:
            report_map[instance_id]["resolved"] = True
        report_map[instance_id]["tests_status"] = report

    with open("/logs/verifier/report.json", "w") as f:
        json.dump(report_map, f, indent=4)

    print("SWEBench results starts here")
    if report_map[instance_id]["resolved"]:
        print("PASSED")
    else:
        print("FAILED")
    print("SWEBench results ends here")

    resolved = report_map[instance_id]["resolved"]
    with open("/logs/verifier/script_reward.txt", "w") as f:
        f.write("1" if resolved else "0")
    with open("/logs/verifier/reward.txt", "w") as f:
        f.write("1" if resolved else "0")
    sys.exit(0 if resolved else 1)


if __name__ == "__main__":
    main()
