"""Expose test programs as executables, including from Python paths with spaces."""
import shlex
import sys


def python_entrypoint(executable, script):
    executable.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(script)]) + ' "$@"\n')
    executable.chmod(0o700)


def python_executable(executable, source):
    script = executable.with_name(executable.name + '.py')
    script.write_text(source)
    python_entrypoint(executable, script)
