import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pressurecooker import cli


class InputTests(unittest.TestCase):
    def test_invalid_numeric_inputs(self):
        for flag, values in [("-t", ["nan", "inf", "0", "-1"]),
                             ("-r", ["nan", "inf", "0", "1001"]),
                             ("-a", ["0", "-1", "1.5"])]:
            for value in values:
                with self.subTest(flag=flag, value=value), self.assertRaises(cli.CliError) as caught:
                    cli.parse_args(["input.mp4", "out.mp4", "-t", "10", flag, value])
                self.assertEqual(caught.exception.code, 2)

    def test_help_and_version_need_no_tools(self):
        with patch.object(cli, "resolve_tool", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            for arguments in (["--help"], ["/?"], ["--version"]):
                self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(cli.main([]), 2)

    def test_missing_tool(self):
        with patch.object(cli.shutil, "which", return_value=None), self.assertRaises(cli.CliError) as caught:
            cli.resolve_tool(None, "ffmpeg")
        self.assertEqual(caught.exception.code, 3)

    @unittest.skipUnless(os.name == "nt", "Windows batch-file behavior")
    def test_windows_batch_tools_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "ffprobe.cmd"
            wrapper.write_text("@echo off\n", encoding="ascii")
            with self.assertRaises(cli.CliError) as caught:
                cli.resolve_tool(str(wrapper), "ffprobe")
            self.assertEqual(caught.exception.code, 3)
            with patch.object(cli.shutil, "which", return_value=str(wrapper)), self.assertRaises(cli.CliError):
                cli.resolve_tool(None, "ffprobe")

    def test_invalid_probe_metadata_is_not_silently_audio_free(self):
        for response in [(1, "", "error"), (0, "not-json", ""),
                         (0, json.dumps({"streams": {}}), "")]:
            with self.subTest(response=response), patch.object(cli, "_capture", return_value=response):
                with self.assertRaises(cli.CliError):
                    cli.probe_audio("ffprobe", "input.mp4")
        with patch.object(cli, "_capture", return_value=(0, '{"streams":[]}', "")):
            self.assertIsNone(cli.probe_audio("ffprobe", "input.mp4"))
        with patch.object(cli, "_capture", return_value=(0, '{"streams":[{}]}', "")):
            self.assertIsNotNone(cli.probe_audio("ffprobe", "input.mp4"))

    def test_invalid_video_dimensions(self):
        for value in ("NaN", "Infinity", "-1", "0"):
            response = json.dumps({"streams": [{"width": value, "height": 144}]})
            with self.subTest(value=value), patch.object(cli, "_capture", return_value=(0, response, "")):
                with self.assertRaises(cli.CliError):
                    cli.probe_dimensions("ffprobe", "input.mp4")

    def test_qsv_failure_selects_matching_software_encoder(self):
        with patch.object(cli, "probe_qsv", return_value=(False, "unsupported driver")):
            encoder = cli.choose_encoder("hevc", "qsv", "ffmpeg")
        self.assertEqual(encoder.name, "libx265")
        self.assertIn("unsupported driver", encoder.warning)

    def test_media_paths_are_argument_list_values(self):
        media = "space %44211323% / $(echo secret); video.mp4"
        with patch.object(cli.subprocess, "run") as execute:
            execute.return_value.returncode = 0
            execute.return_value.stdout = "6"
            execute.return_value.stderr = ""
            cli.probe_duration("ffprobe", media)
        arguments, options = execute.call_args
        self.assertIsInstance(arguments[0], list)
        self.assertEqual(arguments[0][-1], media)
        self.assertFalse(options.get("shell", False))


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pressurecooker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "video %44211323%.mp4"
        self.source.write_bytes(b"source")
        self.output = self.root / "out.mp4"
        self.options = cli.Options(source=str(self.source), destination=str(self.output), target_mib=1)
        self.hardware = cli._Encoder("hevc_qsv", True, "QSV", None)
        self.commands = []

    def run_plan(self, operation, hardware=True):
        encoder = self.hardware if hardware else cli._Encoder("libx264", False, "software", None)
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "probe_duration", return_value=6), \
             patch.object(cli, "probe_audio", return_value=cli._Audio("aac", 96)), \
             patch.object(cli, "probe_dimensions", return_value=(640, 360)), \
             patch.object(cli, "_run_encode", side_effect=operation), \
             contextlib.redirect_stdout(io.StringIO()):
            return cli.run(self.options)

    def successful_encode(self, command, verbose):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"finished")
        return 0, ""

    def assert_no_partial_output(self):
        self.assertEqual(list(self.root.glob(".pressurecooker-*")), [])
        self.assertEqual(self.source.read_bytes(), b"source")

    def test_existing_output_requires_explicit_overwrite(self):
        self.output.write_bytes(b"previous")
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(self.successful_encode)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_failed_encode_preserves_previous_output(self):
        self.output.write_bytes(b"previous")
        self.options.overwrite = True
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(lambda command, verbose: (1, "encoder failed"))
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(self.output.read_bytes(), b"previous")
        self.assert_no_partial_output()

    def test_successful_overwrite(self):
        self.output.write_bytes(b"previous")
        self.options.overwrite = True
        self.assertEqual(self.run_plan(self.successful_encode), 0)
        self.assertEqual(self.output.read_bytes(), b"finished")
        self.assert_no_partial_output()

    def test_concurrent_destination_is_preserved(self):
        def encode(command, verbose):
            self.successful_encode(command, verbose)
            self.output.write_bytes(b"concurrent")
            return 0, ""
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(encode)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.output.read_bytes(), b"concurrent")
        self.assert_no_partial_output()

    def test_source_alias_created_during_encode_is_refused(self):
        self.options.overwrite = True
        def encode(command, verbose):
            self.successful_encode(command, verbose)
            os.link(self.source, self.output)
            return 0, ""
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(encode)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.output.read_bytes(), b"source")
        self.assert_no_partial_output()

    def test_source_hardlink_is_refused_before_encoding(self):
        os.link(self.source, self.output)
        self.options.overwrite = True
        with self.assertRaises(cli.CliError):
            self.run_plan(self.successful_encode)
        self.assertEqual(self.commands, [])
        self.assert_no_partial_output()

    def test_empty_success_output_is_refused(self):
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(lambda command, verbose: (0, ""))
        self.assertEqual(caught.exception.code, 5)
        self.assertFalse(self.output.exists())
        self.assert_no_partial_output()

    def test_interrupt_removes_temporary_output(self):
        def interrupt(command, verbose):
            self.successful_encode(command, verbose)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan(interrupt)
        self.assertFalse(self.output.exists())
        self.assert_no_partial_output()

    def test_empty_pass_statistics_stop_before_second_pass(self):
        self.output.write_bytes(b"previous")
        self.options.overwrite = True
        def encode(command, verbose):
            self.commands.append(command)
            return 0, ""
        with self.assertRaises(cli.CliError) as caught:
            self.run_plan(encode, hardware=False)
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.output.read_bytes(), b"previous")
        self.assert_no_partial_output()

    def test_dry_run_does_not_create_destination_directories(self):
        self.options.destination = str(self.root / "absent" / "out")
        self.options.dry_run = True
        self.assertEqual(self.run_plan(lambda command, verbose: self.fail("dry run encoded")), 0)
        self.assertFalse((self.root / "absent").exists())

    def test_temporary_outputs_are_unique(self):
        self.options.overwrite = True
        self.run_plan(self.successful_encode)
        self.run_plan(self.successful_encode)
        self.assertNotEqual(self.commands[0][-1], self.commands[1][-1])
        self.assert_no_partial_output()


if __name__ == "__main__":
    unittest.main()
