import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import xtract_doi


class LinkInputTests(unittest.TestCase):
    def test_parse_doi_and_url_lines(self):
        dois, errors = xtract_doi.parse_link_line("Paper DOI: 10.1038/s41586-020-00773-7")
        self.assertEqual(dois, ["10.1038/s41586-020-00773-7"])
        self.assertEqual(errors, [])

        urls, errors = xtract_doi.parse_link_line("https://example.org/paper.pdf")
        self.assertEqual(urls, ["https://example.org/paper.pdf"])
        self.assertEqual(errors, [])

    def test_discover_link_files_filters_non_link_docs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "papers.txt").write_text(
                "10.1000/example-a\nhttps://example.org/paper.pdf\n",
                encoding="utf-8",
            )
            (folder / "README.txt").write_text(
                "https://example.org/example\nProject notes\n", encoding="utf-8"
            )
            (folder / "done.txt").write_text(
                "https://example.org/completed.pdf\n", encoding="utf-8"
            )
            found = xtract_doi.discover_link_files(folder)
            self.assertEqual([path.name for path in found], ["papers.txt"])

    def test_select_discovered_file_by_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "a.txt").write_text("10.1000/alpha\n", encoding="utf-8")
            (folder / "b.txt").write_text("10.1000/beta\n", encoding="utf-8")
            with patch("builtins.input", return_value="2"):
                selected = xtract_doi.select_link_files(folder)
            self.assertEqual([path.name for path in selected], ["b.txt"])

    def test_terminal_fallback_accepts_external_file_when_nothing_is_discovered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_folder = root / "project"
            project_folder.mkdir()
            external_file = root / "my links.txt"
            external_file.write_text("10.1000/external\n", encoding="utf-8")
            with patch("builtins.input", return_value=str(external_file)):
                selected = xtract_doi.select_link_files(
                    project_folder, allow_custom_path=True
                )
            self.assertEqual(selected, [external_file.resolve()])

    def test_terminal_fallback_can_switch_from_discovered_file_to_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_folder = root / "project"
            project_folder.mkdir()
            (project_folder / "local.txt").write_text("10.1000/local\n", encoding="utf-8")
            external_file = root / "selected.txt"
            external_file.write_text("10.1000/selected\n", encoding="utf-8")
            with patch("builtins.input", side_effect=["path", str(external_file)]):
                selected = xtract_doi.select_link_files(
                    project_folder, allow_custom_path=True
                )
            self.assertEqual(selected, [external_file.resolve()])

    def test_console_status_output_includes_requested_live_states(self):
        output = StringIO()
        with redirect_stdout(output):
            xtract_doi.show_item_result(1, 1, "10.1000/example", "pending")
            xtract_doi.show_item_result(1, 1, "10.1000/example", "processing")
            xtract_doi.show_item_result(1, 1, "10.1000/example", "ok")
        for label in ("PENDING", "PROCESSING", "SUCCESS"):
            self.assertIn(label, output.getvalue())


class DownloadSettingsTests(unittest.TestCase):
    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            expected = {"download_dir": str(Path(temp_dir) / "my papers")}
            xtract_doi.save_settings(expected, settings_path)
            self.assertEqual(xtract_doi.load_settings(settings_path), expected)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), expected)

    def test_invalid_settings_fall_back_to_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            settings_path.write_text("not json", encoding="utf-8")
            expected = (xtract_doi.SCRIPT_DIR / xtract_doi.DOWNLOAD_DIR).resolve()
            self.assertEqual(
                xtract_doi.load_settings(settings_path)["download_dir"], str(expected)
            )

    def test_folder_is_created_recursively_and_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "nested" / "pdfs"
            result = xtract_doi.prepare_download_directory(target)
            self.assertEqual(result, target.resolve())
            self.assertTrue(result.is_dir())
            self.assertEqual(xtract_doi.prepare_download_directory(result), result)

    def test_file_cannot_be_used_as_download_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "not-a-folder"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(NotADirectoryError):
                xtract_doi.prepare_download_directory(file_path)

    def test_cli_folder_choice_creates_and_remembers_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "created" / "pdfs"
            settings = {"download_dir": str(Path(temp_dir) / "default")}
            with patch("builtins.input", return_value=str(target)), patch.object(
                xtract_doi, "save_settings"
            ) as save:
                result = xtract_doi.choose_download_dir_cli(settings)
            self.assertEqual(result, target.resolve())
            self.assertTrue(result.is_dir())
            save.assert_called_once_with({"download_dir": str(target.resolve())})

    def test_interactive_console_pauses_before_exit(self):
        class InteractiveInput:
            @staticmethod
            def isatty():
                return True

        with patch.dict(os.environ, {"PDF_NO_PAUSE": "0"}), patch.object(
            xtract_doi.sys, "stdin", InteractiveInput()
        ), patch("builtins.input") as input_mock:
            xtract_doi.pause_before_exit()
        input_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
