#!/usr/bin/env python

import argparse
import configparser
import contextlib
import enum
import glob
import os
from pathlib import Path
import pprint
import re
import shlex
import shutil
import subprocess
import time


MAX_TEST_TIME = 60000


class SectionPrinter:
    @contextlib.contextmanager
    def group(self, title: str):
        print(f"{title}:")
        yield


class TestResult(enum.StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NA = "n/a"
    SKIP = "SKIP"


def run_process_with_timeout(cmd: list[str], timeout: int, env: dict[str, str]):
    print(f"Running: {cmd}")
    child = subprocess.Popen(cmd, env=env)
    start = time.time()
    while child.poll() is None:
        if time.time() > start + timeout:
            break
        time.sleep(0.01)
    if child.poll() is None:
        child.kill()
        return TestResult.TIMEOUT
    elif child.returncode == 0:
        return TestResult.SUCCESS
    else:
        return TestResult.FAILED


class GitHubSectionPrinter(SectionPrinter):
    def __init__(self):
        super().__init__()
        self.in_group = False

    @contextlib.contextmanager
    def group(self, title: str):
        print(f"::group::{title}")
        assert not self.in_group, "Can enter a group only once"
        self.in_group = True
        yield
        self.in_group = False
        print("::endgroup::")


def get_unit_tests(prefix: Path) -> dict[str,list[str]]:
    tests = {}
    for test_path in (prefix / "share/installed-tests/SDL3").iterdir():
        config = configparser.ConfigParser()
        config.read(test_path)
        tests[test_path.stem] = shlex.split(config["Test"]["Exec"])
    return tests


def get_automation_cases(source_path: Path) -> list[str]:
    test_cases = []
    for test_path in glob.glob(str(source_path / "test/testautomation*.c")):
        text = open(test_path, "rt").read()
        for m in re.finditer(r"""static\s*(?:const)?\s*SDLTest_TestCaseReference\s*[a-zA-Z0-9_]+\s*=\s*\{\s*[a-zA-Z0-9_]+\s*,\s*"([a-zA-Z0-9_.-]+)"\s*,""", text, flags=re.M):
            test_cases.append(m[1])
    return test_cases


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("-R", default="https://github.com/libsdl-org/SDL", dest="repo", help="SDL repo")
    parser.add_argument("--other-tags", nargs="+", required=True, help="Other git tags to test against")
    parser.add_argument("--dut-tag", required=True, help="Tag to test")
    parser.add_argument("-C", type=Path, dest="cwd", default=Path.cwd(), help="working directory")
    parser.add_argument("--filter-tests", help="Filter tests")
    parser.add_argument("--filter-testautomation", help="Filter testautomation tests (implies --testautomation)")
    parser.add_argument("--clone", action="store_true", help="clone repos")
    parser.add_argument("--build", action="store_true", help="build projects")
    parser.add_argument("--github", action="store_true", help="Run on GitHub")
    parser.add_argument("--testautomation", action="store_true", help="Run testautomation tests separately")
    args = parser.parse_args()

    if args.filter_testautomation:
        args.testautomation = True

    section_printer = GitHubSectionPrinter() if args.github else SectionPrinter()

    TAG_SRC = {}
    TAG_BLD = {}
    TAG_PREF = {}
    TAG_VERSION = {}
    TAG_TESTS = {}
    TAG_AUTOMATION_CASES = {}

    tags = list(args.other_tags) + [args.dut_tag]
    all_test_names = set()
    all_automation_cases = set()

    cwd = args.cwd
    for tag in tags:
        tag_src = cwd / tag
        TAG_SRC[tag] = tag_src
        tag_bld = tag_src / "build"
        tag_bld.mkdir(exist_ok=True, parents=True)
        TAG_BLD[tag] = tag_src
        tag_pref = tag_src / "prefix"
        TAG_PREF[tag] = tag_pref
        if args.clone:
            shutil.rmtree(tag_src, ignore_errors=True)
            tag_src.mkdir(exist_ok=True, parents=True)
            with section_printer.group(f"Cloning '{args.repo}' to '{tag_src}'"):
                subprocess.check_call(["git", "clone", "--depth=1", "-b", tag, args.repo, tag_src])

        version_h = (tag_src / "include/SDL3/SDL_version.h").read_text()
        major = int(next(re.finditer(r"#define\s+SDL_MAJOR_VERSION\s+([0-9]+)", version_h, flags=re.M))[1])
        minor = int(next(re.finditer(r"#define\s+SDL_MINOR_VERSION\s+([0-9]+)", version_h, flags=re.M))[1])
        micro = int(next(re.finditer(r"#define\s+SDL_MICRO_VERSION\s+([0-9]+)", version_h, flags=re.M))[1])
        TAG_VERSION[tag] = (major, minor, micro)
        print(f"Tag {tag} -> {TAG_VERSION[tag]}")

        if args.build:
            shutil.rmtree(tag_pref, ignore_errors=True)
            tag_pref.mkdir(exist_ok=True, parents=True)
            shutil.rmtree(tag_bld, ignore_errors=True)
            tag_bld.mkdir(exist_ok=True, parents=True)
            with section_printer.group(f"Configuring '{args.repo}' in '{tag_bld}'"):
                subprocess.check_call(["cmake", "-GNinja", "-S", tag_src, "-B", tag_bld, f"-DCMAKE_INSTALL_PREFIX={tag_pref}", "-DSDL_SHARED=ON", "-DSDL_STATIC=OFF", "-DSDL_TESTS=ON", "-DSDL_INSTALL_TESTS=ON", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_BINDIR=bin", "-DCMAKE_INSTALL_INCLUDEDIR=include", "-DCMAKE_INSTALL_LIBDIR=lib"])
            with section_printer.group(f"Building '{tag_bld}'"):
                subprocess.check_call(["cmake", "--build", tag_bld, "--config", "Release"])
            with section_printer.group(f"Installing to '{tag_pref}'"):
                subprocess.check_call(["cmake", "--install", tag_bld, "--config", "Release"])

        tag_tests = get_unit_tests(tag_pref)
        print(f"Tag {tag} has {len(tag_tests)} tests")
        all_test_names.update(tag_tests.keys())
        TAG_TESTS[tag] = tag_tests

        automation_cases = get_automation_cases(tag_src)
        print(f"Tag {tag} has {len(automation_cases)} testautomation tests")
        if args.filter_testautomation:
            automation_cases = [t for t in automation_cases if args.filter_testautomation in t]
            print(f"Filter reduced number of testautomation tests to {len(automation_cases)}")
        all_automation_cases.update(automation_cases)
        TAG_AUTOMATION_CASES[tag] = automation_cases

    child_environ = dict(os.environ)
    child_environ["DYLD_LIBRARY_PATH"] = str(TAG_PREF[args.dut_tag] / "lib")
    child_environ["LD_LIBRARY_PATH"] = str(TAG_PREF[args.dut_tag] / "lib")
    child_environ["PATH"] = str(TAG_PREF[args.dut_tag] / "bin") + os.path.pathsep + child_environ["PATH"]

    TEST_RESULTS = {}
    TESTAUTOMATION_RESULTS = {}

    print("Child environment:")
    pprint.pprint(child_environ)

    success = True
    for other_tag in args.other_tags:
        if not (TAG_VERSION[other_tag] <= TAG_VERSION[args.dut_tag]):
            print(f"ERROR: Version of {other_tag} {TAG_VERSION[other_tag]} is not <= version of {args.dut_tag} {TAG_VERSION[args.dut_tag]}")
            success = False
            continue

        other_tag_test_results = {}
        TEST_RESULTS[other_tag] = other_tag_test_results

        print(f"Running test executables of {other_tag} {TAG_VERSION[other_tag]}")
        for test_name, test_command in TAG_TESTS[other_tag].items():
            with section_printer.group(f"Run {test_name}: {test_command}"):
                if args.filter_tests and args.filter_tests not in test_name:
                    print("Skipping")
                    test_result = TestResult.SKIP
                else:
                    test_result = run_process_with_timeout(cmd=test_command, timeout=MAX_TEST_TIME, env=child_environ)
                    print(f"Test {test_name} : {test_result}")
                other_tag_test_results[test_name] = test_result
                success = success and test_result == TestResult.SUCCESS and test_result not in (TestResult.NA, TestResult.SKIP)

        other_tag_testautomation_results = {}
        TESTAUTOMATION_RESULTS[other_tag] = other_tag_testautomation_results

        if args.testautomation:
            if "testautomation" in TAG_TESTS[other_tag]:
                testautomation_cmd = TAG_TESTS[other_tag]["testautomation"]
                for testautomation_case in TAG_AUTOMATION_CASES[other_tag]:
                    with section_printer.group(f"Run testautomation --filter {testautomation_case}"):
                        if args.filter_testautomation and args.filter_testautomation not in testautomation_case:
                            print("Skipping")
                            test_result = TestResult.SKIP
                        else:
                            unit_test_cmd = testautomation_cmd + ["--filter", testautomation_case]
                            test_result = run_process_with_timeout(cmd=unit_test_cmd, timeout=MAX_TEST_TIME, env=child_environ)
                            print(f"Test testautomation --filter {testautomation_case} : {test_result}")
                        other_tag_testautomation_results[testautomation_case] = test_result
                success = success and test_result == TestResult.SUCCESS and test_result not in (TestResult.NA, TestResult.SKIP)

    if args.testautomation:
        max_testautomation_name = max(len(n) for n in all_automation_cases)

        print("")
        print("Testautomation results:")
        title1 = "|".rjust(max_testautomation_name+1) + "|".join(f"{other_tag:^12}" for other_tag in args.other_tags)
        title2 = "|".rjust(max_testautomation_name+1) + "|".join(f"{str(TAG_VERSION[other_tag][0])+'.'+str(TAG_VERSION[other_tag][1])+'.'+str(TAG_VERSION[other_tag][2]):^12}" for other_tag in args.other_tags)
        print(title1)
        print(title2)
        print("-"*len(title1))
        for test_name in all_automation_cases:
            print(test_name.ljust(max_testautomation_name) + "|" + "|".join(f"{r:^12}" for r in tuple(TESTAUTOMATION_RESULTS[other_tag].get(test_name, TestResult.NA) for other_tag in args.other_tags)))

    if all_test_names:
        max_len_test_name = max(len(n) for n in all_test_names)

        print()
        print("Test results:")
        title1 = "|".rjust(max_len_test_name+1) + "|".join(f"{other_tag:^12}" for other_tag in args.other_tags)
        title2 = "|".rjust(max_len_test_name+1) + "|".join(f"{str(TAG_VERSION[other_tag][0])+'.'+str(TAG_VERSION[other_tag][1])+'.'+str(TAG_VERSION[other_tag][2]):^12}" for other_tag in args.other_tags)
        print(title1)
        print(title2)
        print("-"*len(title1))
        for test_name in all_test_names:
            print(test_name.ljust(max_len_test_name) + "|" + "|".join(f"{r:^12}" for r in tuple(TEST_RESULTS[other_tag].get(test_name, TestResult.NA) for other_tag in args.other_tags)))
    else:
        print("No tests")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
