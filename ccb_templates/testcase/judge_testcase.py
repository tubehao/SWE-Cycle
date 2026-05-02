#!/usr/bin/env python3
"""
Language-aware regex judge for TestCase evaluation.

Dispatches to language-specific patterns first; falls back to trying all
patterns when the language is unknown or when the specific patterns miss.

Supported languages / frameworks:
  py     → pytest, Django (unittest), sympy bin/test
  js, ts → Jest, Mocha, Vitest
  java   → JUnit / Maven / Gradle
  go     → go test
  rs     → cargo test
  rb     → RSpec, Minitest
  php    → PHPUnit
  c, cpp → Google Test / CTest (+ fallback to all)
"""
import re, sys

# ── per-framework matchers ──────────────────────────────────────────────

def _pytest(content: str):
    """pytest: '=== 3 passed, 1 failed in 0.12s ===' (also handles ANSI-stripped output)"""
    # Strip ANSI escape codes before matching
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[mKGHfABCDsuJSTl]|\x1b\][^\x07]*\x07')
    clean = ansi_escape.sub('', content)
    m_p = re.search(r'(\d+)\s+passed', clean)
    m_f = re.search(r'(\d+)\s+failed', clean)
    if m_p or m_f:
        p = int(m_p.group(1)) if m_p else 0
        f = int(m_f.group(1)) if m_f else 0
        return p > 0 and f == 0
    return None  # not matched

def _django(content: str):
    """Django unittest: 'Ran X tests in Y.Ys' + 'OK'"""
    m = re.search(r'Ran\s+(\d+)\s+tests?\s+in\s+[\d.]+s', content)
    if m and int(m.group(1)) > 0:
        return (bool(re.search(r'\bOK\b', content))
                and not bool(re.search(r'\bFAIL(ED)?\b|\bERROR\b', content)))
    return None

def _sympy(content: str):
    """sympy bin/test: '[N tests OK]' / '[N tests FAILED]' / 'DO *NOT* COMMIT'"""
    # sympy bin/test prints a summary line like: [33 tests OK] or [2 tests FAILED]
    m_ok   = re.search(r'\[(\d+)\s+tests?\s+OK\]', content)
    m_fail = re.search(r'\[(\d+)\s+tests?\s+FAILED\]', content)
    # "DO *NOT* COMMIT" also signals test failure in sympy
    if re.search(r'DO \*NOT\* COMMIT', content):
        return False
    if m_ok or m_fail:
        ok   = int(m_ok.group(1))   if m_ok   else 0
        fail = int(m_fail.group(1)) if m_fail else 0
        return ok > 0 and fail == 0
    return None

def _gotest(content: str):
    """go test: 'ok  pkg  0.032s' / 'FAIL pkg 0.015s'"""
    if re.search(r'^ok\s+\S+', content, re.MULTILINE):
        return not bool(re.search(r'^FAIL\s', content, re.MULTILINE))
    if re.search(r'^FAIL\s', content, re.MULTILINE):
        return False
    return None

def _jest(content: str):
    """Jest / Vitest: 'Tests: 1 failed, 5 passed, 6 total'"""
    m = re.search(r'Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed', content)
    if m:
        return int(m.group(1) or 0) == 0
    return None

def _mocha(content: str):
    """Mocha: '11 passing (2s)' / '1 failing'"""
    m = re.search(r'(\d+)\s+passing', content)
    if m and int(m.group(1)) > 0:
        return not bool(re.search(r'(\d+)\s+failing', content))
    return None

def _junit(content: str):
    """JUnit / Maven / Gradle: 'Tests run: X, Failures: Y, Errors: Z'"""
    m = re.search(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)', content)
    if m:
        return int(m.group(1)) > 0 and int(m.group(2)) == 0 and int(m.group(3)) == 0
    return None

def _cargo(content: str):
    """cargo test: 'test result: ok. 15 passed; 0 failed'
    Multi-crate workspaces emit multiple 'test result:' lines; aggregate all of them.
    A crate with 0 passed (all filtered) is ignored. Any failed > 0 is a hard failure.
    """
    matches = re.findall(r'test result: (\w+)\.\s+(\d+) passed;\s+(\d+) failed', content)
    if not matches:
        return None
    has_failure = any(int(f) > 0 for _, _, f in matches)
    has_pass    = any(s == "ok" and int(p) > 0 for s, p, _ in matches)
    return has_pass and not has_failure

def _rspec(content: str):
    """RSpec: '20 examples, 0 failures'"""
    m = re.search(r'(\d+)\s+examples?,\s+(\d+)\s+failures?', content)
    if m:
        return int(m.group(1)) > 0 and int(m.group(2)) == 0
    return None

def _phpunit(content: str):
    """PHPUnit: 'OK (12 tests, 30 assertions)' or 'OK, but some tests were skipped!'"""
    # Strict form: OK (N tests, M assertions)
    m = re.search(r'OK\s+\((\d+)\s+tests?,\s+(\d+)\s+assertions?\)', content)
    if m:
        return int(m.group(1)) > 0
    # Loose form (skipped/deprecation warnings): 'Tests: N, Assertions: M' without FAILURES
    m2 = re.search(r'Tests:\s+(\d+),\s+Assertions:\s+(\d+)', content)
    if m2:
        return int(m2.group(1)) > 0 and not bool(re.search(r'FAILURES!|Failures:\s+[1-9]', content))
    if re.search(r'FAILURES!', content):
        return False
    return None

def _minitest(content: str):
    """Ruby Minitest / test-unit: '8 runs, 15 assertions, 0 failures, 0 errors'
    The test-unit gem (used in many Ruby projects) outputs 'N tests,' instead of 'N runs,';
    accept both forms.
    """
    m = re.search(r'(\d+)\s+(?:runs?|tests?),\s+(\d+)\s+assertions?,\s+(\d+)\s+failures?,\s+(\d+)\s+errors?', content)
    if m:
        return int(m.group(1)) > 0 and int(m.group(3)) == 0 and int(m.group(4)) == 0
    return None

def _gtest(content: str):
    """Google Test / CTest: '[  PASSED  ] X tests.' / '[  FAILED  ] X tests.'"""
    m_p = re.search(r'\[\s*PASSED\s*\]\s*(\d+)\s+tests?', content)
    m_f = re.search(r'\[\s*FAILED\s*\]\s*(\d+)\s+tests?', content)
    if m_p or m_f:
        p = int(m_p.group(1)) if m_p else 0
        f = int(m_f.group(1)) if m_f else 0
        return p > 0 and f == 0
    return None

def _doctest(content: str):
    """doctest (C++): '<OverallResultsTestCases successes="N" failures="M"/>'"""
    m = re.search(r'<OverallResultsTestCases\s+successes="(\d+)"\s+failures="(\d+)"', content)
    if m:
        return int(m.group(1)) > 0 and int(m.group(2)) == 0
    return None

def _tcl_redis(content: str):
    """Redis TCL test runner: '\\o/ All tests passed without errors!'"""
    if re.search(r'All tests passed without errors', content):
        return not bool(re.search(r'!!! WARNING The following tests failed:', content))
    if re.search(r'!!! WARNING The following tests failed:', content):
        return False
    return None

def _karma(content: str):
    """Karma (JS): 'SUMMARY: ✔ 12 tests completed' / '✗ 2 tests failed'"""
    m = re.search(r'(\d+)\s+tests?\s+completed', content)
    if m and int(m.group(1)) > 0:
        return not bool(re.search(r'(\d+)\s+tests?\s+failed', content))
    m2 = re.search(r'(\d+)\s+tests?\s+failed', content)
    if m2 and int(m2.group(1)) > 0:
        return False
    return None

def _gradle(content: str):
    """Gradle test results: '| Results: SUCCESS (N tests, N passed, 0 failed, 0 skipped) |'
    Multi-module builds emit one line per module; aggregate all SUCCESS lines.
    """
    successes = re.findall(
        r'Results: SUCCESS \((\d+) tests, (\d+) passed, (\d+) failed', content)
    failures  = re.findall(
        r'Results: FAILURE \((\d+) tests, (\d+) passed, (\d+) failed', content)
    if not successes and not failures:
        return None
    any_pass = any(int(p) > 0 for _, p, _ in successes)
    any_fail = any(int(f) > 0 for _, _, f in successes + failures)
    return any_pass and not any_fail

# ── language → ordered matcher list ─────────────────────────────────────

LANG_MATCHERS = {
    "py":   [_pytest, _django, _sympy],
    "js":   [_jest, _mocha, _karma],
    "ts":   [_jest, _mocha, _karma],
    "java": [_junit, _gradle],
    "go":   [_gotest],
    "rs":   [_cargo],
    "rust": [_cargo],
    "rb":   [_rspec, _minitest],
    "ruby": [_rspec, _minitest],
    "php":  [_phpunit],
    "c":    [_gtest, _doctest, _tcl_redis],
    "cpp":  [_gtest, _doctest],
}

ALL_MATCHERS = [
    _pytest, _django, _sympy, _gotest, _jest, _mocha, _karma,
    _junit, _gradle, _cargo, _rspec, _phpunit, _minitest,
    _gtest, _doctest, _tcl_redis,
]

# ── main entry ──────────────────────────────────────────────────────────

def judge(log_path: str, language: str = "") -> bool:
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    lang = language.lower().strip()
    matchers = LANG_MATCHERS.get(lang, None)

    if matchers:
        # Try language-specific matchers first
        for fn in matchers:
            result = fn(content)
            if result is not None:
                return result
        # Language-specific matchers didn't match — fall through to all
        print(f"[judge] language={lang}: specific matchers missed, trying all")

    # Fallback: try every matcher
    for fn in ALL_MATCHERS:
        result = fn(content)
        if result is not None:
            return result

    return False

if __name__ == "__main__":
    log_file = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else ""
    resolved = judge(log_file, lang)
    print(f"Judge result: {'RESOLVED' if resolved else 'NOT RESOLVED'} (language={lang!r})")
    sys.exit(0 if resolved else 1)
