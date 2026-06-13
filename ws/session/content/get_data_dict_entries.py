#!/usr/bin/env python3
"""Compatibility CLI for resolving rosotacom address expressions."""

import argparse

try:
    from .address_resolution import main as resolve_address_expressions
except ImportError:
    from address_resolution import main as resolve_address_expressions


main = resolve_address_expressions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve rosotacom address expressions.")
    parser.add_argument("-k", "--key_string", required=True)
    args = parser.parse_args()
    print(";".join(resolve_address_expressions(args.key_string)))
