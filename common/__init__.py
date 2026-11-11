"""Shared support code for the four modules.

This package holds the few things every module must agree on and none of them
should own alone: where a secret comes from, how a log line is shaped, what
hours the framework itself works in, and how the operator is told about it.

It is deliberately not a fifth module. It holds no state, talks to no broker,
and makes no trading decision: every function here is a small shared library
used by Module 1, Module 2, Module 3, Module 4 and the runner.
"""
