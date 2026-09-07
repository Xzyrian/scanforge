"""Developer tools that ship inside the frozen build.

This file exists so `tools` is a real package rather than a namespace one.
PyInstaller resolves `from tools.freeze_check import selftest` reliably for a
regular package; with a namespace package it can leave the module out and the
built exe then fails `--selftest` with ModuleNotFoundError - which looks like a
broken build script rather than a packaging gap.
"""
