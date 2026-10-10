#!/usr/bin/env python3
"""Unit tests for new-repo provisioning utility."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from new_repo import SecretProvider, parse_args


class TestArgParsing(unittest.TestCase):
    """Test CLI argument parsing and default values."""

    def test_default_arguments(self) -> None:
        import sys

        sys.argv = ["new-repo", "sample-project"]
        args = parse_args()
        self.assertEqual(args.name, "sample-project")
        self.assertFalse(args.public)
        self.assertEqual(args.license, "UNLICENSE")
        self.assertIsNone(args.template)
        self.assertFalse(args.no_mirror)
        self.assertFalse(args.no_clone)

    def test_omitted_name_defaults_to_none(self) -> None:
        import sys

        sys.argv = ["new-repo"]
        args = parse_args()
        self.assertIsNone(args.name)

    def test_custom_flags(self) -> None:
        import sys

        sys.argv = [
            "new-repo",
            "open-api",
            "--public",
            "-d",
            "My public API",
            "-l",
            "MIT",
            "-t",
            "template-fastapi",
            "-M",
            "--no-clone",
        ]
        args = parse_args()
        self.assertEqual(args.name, "open-api")
        self.assertTrue(args.public)
        self.assertEqual(args.description, "My public API")
        self.assertEqual(args.license, "MIT")
        self.assertEqual(args.template, "template-fastapi")
        self.assertTrue(args.no_mirror)
        self.assertTrue(args.no_clone)


class TestDotenvLoader(unittest.TestCase):
    """Test parsing of local .env files without external dependencies."""

    def test_load_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_file = Path(tmpdir) / ".env"
            dotenv_file.write_text(
                "# Sample configuration\n"
                "GITEA_TOKEN=test-token-123\n"
                'GITHUB_TOKEN="gh-token-456"\n'
                "GITEA_USER='custom-user'\n"
                "\n"
                "INVALID_LINE\n"
            )
            loaded = SecretProvider._load_dotenv(dotenv_file)
            self.assertEqual(loaded.get("GITEA_TOKEN"), "test-token-123")
            self.assertEqual(loaded.get("GITHUB_TOKEN"), "gh-token-456")
            self.assertEqual(loaded.get("GITEA_USER"), "custom-user")


if __name__ == "__main__":
    unittest.main()
