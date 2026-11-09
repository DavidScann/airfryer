import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airfryer import cli


class ArgumentTests(unittest.TestCase):
    def test_no_arguments_prints_usage_and_fails(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([]), 2)
        self.assertIn("airfryer - deep fry a video into a degraded mess", output.getvalue())

    def test_source_and_destination_are_the_only_arguments(self):
        self.assertEqual(cli._positionals(["in.mp4", "out.mp4"]), ("in.mp4", "out.mp4"))

    def test_unknown_option_is_rejected(self):
        self.assertEqual(cli.main(["in.mp4", "out.mp4", "--target-mb", "5"]), 2)

    def test_extra_argument_is_rejected(self):
        self.assertEqual(cli.main(["in.mp4", "out.mp4", "extra.mp4"]), 2)


class FryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airfryer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "video %44211323%.mp4"
        self.source.write_bytes(b"source")
        self.output = self.root / "out.mp4"
        self.commands = []

    def _encode(self, command, verbose):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"finished")
        return 0, ""

    def _vulkan_encoder(self):
        encoder = cli._Encoder("hevc_vulkan", True, "hardware Vulkan, single-pass bitrate control", None, 1, 0, "Discrete GPU")
        encoder.decode = True
        return encoder

    def _fry(self, encoder):
        self.commands = []
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "_run_encode", side_effect=self._encode), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.run(str(self.source), str(self.output))
        self.assertEqual(code, 0)
        return self.commands

    def test_vulkan_fry_command_encodes_once_at_low_bitrate(self):
        command = self._fry(self._vulkan_encoder())[0]
        self.assertEqual(len(self.commands), 1)
        self.assertLess(command.index("-init_hw_device"), command.index("-i"))
        self.assertLess(command.index("-hwaccel"), command.index("-i"))
        self.assertIn("vulkan=pc:1", command)
        self.assertIn("scale_vulkan=format=nv12:w=320:h=240,fps=12,format=yuv420p", command)
        self.assertEqual(command[command.index("-quality") + 1], "0")
        self.assertEqual(command[command.index("-rc_mode") + 1], "auto")
        self.assertEqual(command[command.index("-b:v") + 1], "250k")
        self.assertEqual(command[command.index("-c:a") + 1], "libmp3lame")
        self.assertEqual(command[command.index("-b:a") + 1], "32k")
        self.assertEqual(command[command.index("-af") + 1], "volume=2.0,loudnorm=I=-16")
        self.assertEqual(command[command.index("-f") + 1], "mp4")
        self.assertEqual(self.output.read_bytes(), b"finished")
        self.assertEqual(list(self.root.glob(".airfryer-*")), [])

    def test_software_fry_command_low_bitrate_and_range(self):
        command = self._fry(cli._Encoder("libx265", False, "software, 2-pass ABR", None))[0]
        self.assertIn("libx265", command)
        self.assertIn("scale=320:240,fps=12,format=yuv420p", command)
        self.assertEqual(command[command.index("-b:v") + 1], "250k")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-c:a") + 1], "libmp3lame")

    def test_existing_destination_is_refused(self):
        self.output.write_bytes(b"previous")
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=self._vulkan_encoder()), \
             patch.object(cli, "_run_encode") as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.CliError) as caught:
                cli.run(str(self.source), str(self.output))
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_missing_source_is_rejected(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.run(str(self.root / "missing.mp4"), str(self.output))
        self.assertEqual(caught.exception.code, 2)

    def test_source_may_not_be_destination(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.run(str(self.source), str(self.source))
        self.assertEqual(caught.exception.code, 2)

    def test_encode_failure_raises_exit_code_5(self):
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=self._vulkan_encoder()), \
             patch.object(cli, "_run_encode", return_value=(1, "boom")) as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.CliError) as caught:
                cli.run(str(self.source), str(self.output))
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(execute.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_empty_encode_output_raises_exit_code_5(self):
        def encode_empty(command, verbose):
            return 0, ""
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=self._vulkan_encoder()), \
             patch.object(cli, "_run_encode", side_effect=encode_empty), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.CliError) as caught:
                cli.run(str(self.source), str(self.output))
        self.assertEqual(caught.exception.code, 5)
        self.assertFalse(self.output.exists())

    def test_interrupt_cleans_temporary_output(self):
        def encode_interrupt(command, verbose):
            raise KeyboardInterrupt
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=self._vulkan_encoder()), \
             patch.object(cli, "_run_encode", side_effect=encode_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(str(self.source), str(self.output))
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".airfryer-*")), [])

    def test_directory_destination_gets_fry_name(self):
        destination = self.root / "out"
        destination.mkdir()
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=self._vulkan_encoder()), \
             patch.object(cli, "_run_encode", side_effect=self._encode), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.run(str(self.source), str(destination))
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "out" / "video %44211323%.fry.mp4").exists())
        self.assertEqual(list(self.root.glob(".airfryer-*")), [])


if __name__ == "__main__":
    unittest.main()
