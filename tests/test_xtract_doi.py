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

    def test_realtime_aggregate_counts_update_for_each_result(self):
        counts = xtract_doi.new_queue_counts(4)
        xtract_doi.apply_queue_transition(counts, "processing")
        self.assertEqual((counts["pending"], counts["processing"]), (3, 1))
        xtract_doi.apply_queue_transition(counts, "pending")
        self.assertEqual((counts["pending"], counts["processing"]), (4, 0))
        for result in ("ok", "skip", "missing", "error"):
            xtract_doi.apply_queue_transition(counts, "processing")
            xtract_doi.apply_queue_transition(counts, result)
        self.assertEqual(
            counts,
            {
                "total": 4,
                "pending": 0,
                "processing": 0,
                "success": 1,
                "skipped": 1,
                "missing": 1,
                "failed": 1,
            },
        )

    def test_dashboard_shows_progress_current_doi_and_live_counts(self):
        counts = xtract_doi.new_queue_counts(2)
        output = StringIO()
        with redirect_stdout(output):
            dashboard = xtract_doi.TerminalDashboard()
            dashboard.render(counts, status="PREPARING", detail="Starting browser")
            xtract_doi.apply_queue_transition(counts, "processing")
            dashboard.render(
                counts,
                status="PROCESSING",
                position=1,
                link="10.1000/example",
            )
            xtract_doi.apply_queue_transition(counts, "ok")
            dashboard.render(
                counts,
                status="SUCCESS",
                position=1,
                link="10.1000/example",
                detail="saved paper.pdf",
                log_entry=("SUCCESS", 1, "10.1000/example", "paper.pdf | 829 KB"),
            )
        rendered = output.getvalue()
        self.assertIn("DOI PDF HUNTER", rendered)
        self.assertTrue("Processing 1" in rendered or "PROC 1" in rendered)
        self.assertTrue("Pending 1" in rendered or "PEND 1" in rendered)
        self.assertTrue("Success 1" in rendered or "SUCCESS 1" in rendered)
        self.assertIn("PROCESSING", rendered)
        self.assertIn("1/2", rendered)
        self.assertIn("Searching PDF", rendered)
        self.assertIn("10.1000/example", rendered)
        self.assertIn("RECENT RESULTS", rendered)
        self.assertIn("paper.pdf | 829 KB", rendered)
        self.assertIn("                    _.:._", rendered)
        self.assertIn("░", rendered)

    def test_dashboard_recent_results_show_saved_and_missing_logs(self):
        counts = xtract_doi.new_queue_counts(2)
        output = StringIO()
        with redirect_stdout(output):
            dashboard = xtract_doi.TerminalDashboard()
            xtract_doi.apply_queue_transition(counts, "processing")
            xtract_doi.apply_queue_transition(counts, "ok")
            dashboard.render(
                counts,
                status="SUCCESS",
                position=1,
                link="10.1000/saved",
                detail="paper.pdf | 829 KB",
                log_entry=("SUCCESS", 1, "10.1000/saved", "paper.pdf | 829 KB"),
            )
            xtract_doi.apply_queue_transition(counts, "processing")
            xtract_doi.apply_queue_transition(counts, "missing")
            dashboard.render(
                counts,
                status="MISSING",
                position=2,
                link="10.1000/missing",
                detail="not available on this page",
                log_entry=("MISSING", 2, "10.1000/missing", "not available on this page"),
            )
        rendered = output.getvalue()
        self.assertIn("SUCCESS", rendered)
        self.assertIn("paper.pdf | 829 KB", rendered)
        self.assertIn("MISSING", rendered)
        self.assertIn("not available on this page", rendered)
        self.assertEqual(len(dashboard.logs), 2)

    def test_dashboard_redraws_same_region_on_interactive_terminal(self):
        class FakeTerminal(StringIO):
            @staticmethod
            def isatty():
                return True

        terminal = FakeTerminal()
        counts = xtract_doi.new_queue_counts(1)
        with patch.object(xtract_doi.sys, "stdout", terminal):
            dashboard = xtract_doi.TerminalDashboard()
            dashboard.ansi = True
            dashboard.render(counts, status="READY")
            xtract_doi.apply_queue_transition(counts, "processing")
            dashboard.render(counts, status="PROCESSING", position=1, link="10.1000/test")
        rendered = terminal.getvalue()
        self.assertIn("\x1b[2J\x1b[H", rendered)


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
