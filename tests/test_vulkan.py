import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airfryer import cli


GPU_LIST = """[Vulkan @ 123] GPU listing:
[Vulkan @ 123]     0: Integrated GPU (integrated) (0x1234)
[Vulkan @ 123]     1: Discrete GPU (RADV) (discrete) (0x5678)
"""
ENCODE_FAILURE = "[hevc_vulkan @ 456] Device does not support the VK_KHR_video_encode_queue extension!\n"
ENCODE_SUCCESS = "[hevc_vulkan @ 456] Encoder max quality: 3\n"


class VulkanDetectionTests(unittest.TestCase):
    def test_off_skips_gpu_probes(self):
        with patch.object(cli, "_hardware_probe", side_effect=AssertionError):
            self.assertEqual(cli.choose_encoder("hevc", "off", "ffmpeg").name, "libx265")

    def test_selects_second_gpu_when_first_cannot_encode(self):
        with patch.object(cli, "_hardware_probe", side_effect=[
                (False, GPU_LIST + ENCODE_FAILURE), (True, ENCODE_SUCCESS)]) as probe:
            encoder = cli.choose_encoder("hevc", "auto", "ffmpeg")
        self.assertEqual(encoder.name, "hevc_vulkan")
        self.assertEqual(encoder.device, 1)
        self.assertEqual(encoder.device_name, "Discrete GPU (RADV)")
        self.assertTrue(encoder.hardware)
        self.assertIsNone(encoder.warning)
        command = probe.call_args_list[1].args[0]
        self.assertIn("vulkan=pc:1", command)
        self.assertIn("format=nv12,hwupload", command)
        self.assertEqual(command[-1], os.devnull)

    def test_all_gpu_failures_select_matching_software_encoder(self):
        with patch.object(cli, "_hardware_probe", side_effect=[
                (False, GPU_LIST + ENCODE_FAILURE), (False, ENCODE_FAILURE)]), \
                patch.object(cli, "probe_qsv", side_effect=AssertionError):
            encoder = cli.choose_encoder("hevc", "auto", "ffmpeg")
        self.assertEqual(encoder.name, "libx265")
        self.assertFalse(encoder.hardware)
        self.assertIn("GPU 0", encoder.warning)
        self.assertIn("GPU 1", encoder.warning)
        self.assertIn("VK_KHR_video_encode_queue", encoder.warning)
        self.assertNotIn("GPU listing", encoder.warning)

    def test_missing_vulkan_encoder_does_not_probe_every_gpu(self):
        with patch.object(cli, "_hardware_probe", return_value=(
                False, GPU_LIST + "Unknown encoder 'av1_vulkan'\n")) as probe:
            encoder = cli.choose_encoder("av1", "vulkan", "ffmpeg")
        self.assertEqual(encoder.name, "libsvtav1")
        self.assertIn("Unknown encoder", encoder.warning)
        self.assertEqual(probe.call_count, 1)

    def test_quality_is_capped_below_driver_level_count(self):
        with patch.object(cli, "_hardware_probe", side_effect=[
                (True, GPU_LIST + ENCODE_SUCCESS), (True, ENCODE_SUCCESS)]) as probe:
            encoder = cli.choose_encoder("hevc", "auto", "ffmpeg", "placebo")
        self.assertEqual(encoder.quality, 2)
        command = probe.call_args_list[1].args[0]
        self.assertEqual(command[command.index("-quality") + 1], "2")

    def test_failed_quality_probe_retains_working_gpu_at_level_zero(self):
        with patch.object(cli, "_hardware_probe", side_effect=[
                (True, GPU_LIST + ENCODE_SUCCESS), (False, "Unsupported quality level\n")]):
            encoder = cli.choose_encoder("hevc", "auto", "ffmpeg", "slow")
        self.assertTrue(encoder.hardware)
        self.assertEqual(encoder.quality, 0)
        self.assertIn("using quality 0", encoder.warning)

    def test_missing_quality_capability_log_uses_working_level_zero(self):
        with patch.object(cli, "_hardware_probe", return_value=(True, GPU_LIST)) as probe:
            encoder = cli.choose_encoder("hevc", "auto", "ffmpeg", "slow")
        self.assertEqual(encoder.quality, 0)
        self.assertEqual(probe.call_count, 1)

    def test_named_presets_and_av1_numeric_direction(self):
        for preset, quality in [("ultrafast", 0), ("fast", 0), ("medium", 1),
                                ("slow", 2), ("placebo", 5)]:
            for codec in ("h264", "hevc", "av1"):
                with self.subTest(preset=preset, codec=codec):
                    self.assertEqual(cli.resolve_vulkan_quality(preset, codec), quality)
        self.assertEqual(cli.resolve_vulkan_quality("0", "av1"), 8)
        self.assertEqual(cli.resolve_vulkan_quality("7", "av1"), 1)
        self.assertEqual(cli.resolve_vulkan_quality("13", "av1"), 0)

    def test_invalid_presets_fail_before_gpu_probe(self):
        for codec, preset in [("hevc", "7"), ("av1", "14"), ("h264", "invalid")]:
            with self.subTest(codec=codec, preset=preset), \
                    patch.object(cli, "_hardware_probe", side_effect=AssertionError), \
                    self.assertRaises(cli.CliError) as caught:
                cli.choose_encoder(codec, "auto", "ffmpeg", preset)
            self.assertEqual(caught.exception.code, cli.EXIT_USAGE)

    def test_probe_timeout_and_missing_loader_are_recoverable(self):
        for failure, expected in [(subprocess.TimeoutExpired(["ffmpeg"], 30), "timed out"),
                                  (OSError("Vulkan loader missing"), "Vulkan loader missing")]:
            with self.subTest(failure=failure), patch.object(cli.subprocess, "run", side_effect=failure):
                encoder = cli.choose_encoder("hevc", "auto", "ffmpeg")
            self.assertFalse(encoder.hardware)
            self.assertIn(expected, encoder.warning)

    def test_decode_probe_keeps_paths_as_arguments_and_discards_output(self):
        source = "space %44211323% / $(echo secret); video.mp4"
        encoder = cli._Encoder("hevc_vulkan", True, "Vulkan", None, 1, 0, "GPU")
        with patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="", stderr="")) as execute:
            ok, error = cli.probe_vulkan_decode("ffmpeg", source, encoder, ["scale=1280:720", "fps=30"], 1000)
        self.assertTrue(ok)
        self.assertIsNone(error)
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], source)
        self.assertLess(command.index("-hwaccel"), command.index("-i"))
        self.assertIn("vulkan=pc:1", command)
        filters = command[command.index("-vf") + 1]
        self.assertTrue(filters.startswith("scale_vulkan=format=nv12:w=1280:h=720"))
        self.assertIn("hwdownload,format=nv12,fps=30", filters)
        self.assertTrue(filters.endswith("format=nv12,hwupload"))
        self.assertEqual(command[-1], os.devnull)
        self.assertEqual(execute.call_args.kwargs["timeout"], 30)
        self.assertFalse(execute.call_args.kwargs.get("shell", False))


class VulkanCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airfryer-vulkan-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "video %44211323%.mp4"
        self.source.write_bytes(b"source")
        self.output = self.root / "out.mp4"
        self.encoder = cli._Encoder("hevc_vulkan", True, "hardware Vulkan, single-pass bitrate control", None, 1, 0, "Discrete GPU")
        self.commands = []

    def _encode(self, command, verbose):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"encoded")
        return 0, ""

    def _fry(self, decode):
        self.encoder.decode = decode
        self.commands = []
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
                patch.object(cli, "choose_encoder", return_value=self.encoder), \
                patch.object(cli, "probe_vulkan_decode", return_value=(decode, None if decode else "unsupported")), \
                patch.object(cli, "_run_encode", side_effect=self._encode), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.run(str(self.source), str(self.output))
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_vulkan_fry_encode_pipeline_is_single_pass_on_selected_device(self):
        output = self._fry(True)
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertLess(command.index("-init_hw_device"), command.index("-i"))
        self.assertLess(command.index("-hwaccel"), command.index("-i"))
        self.assertIn("vulkan=pc:1", command)
        filters = command[command.index("-vf") + 1]
        self.assertTrue(filters.startswith("scale_vulkan=format=nv12:w=320:h=240"))
        self.assertIn("hwdownload,format=nv12", filters)
        self.assertTrue(filters.endswith("format=nv12,hwupload"))
        self.assertEqual(command[command.index("-quality") + 1], "0")
        self.assertEqual(command[command.index("-rc_mode") + 1], "auto")
        self.assertEqual(command[command.index("-b:v") + 1], "250k")
        self.assertEqual(command[command.index("-c:a") + 1], "libmp3lame")
        for option in ("-preset", "-look_ahead", "-pass:v", "-passlogfile:v"):
            self.assertNotIn(option, command)
        self.assertEqual(self.output.read_bytes(), b"encoded")
        self.assertEqual(list(self.root.glob(".airfryer-*")), [])

    def test_decode_failure_keeps_vulkan_encoding_and_uploads_cpu_frames(self):
        output = self._fry(False)
        self.assertEqual(len(self.commands), 1)
        command = self.commands[0]
        self.assertNotIn("-hwaccel", command)
        self.assertIn("hevc_vulkan", command)
        filters = command[command.index("-vf") + 1]
        self.assertIn("scale=320:240", filters)
        self.assertIn("setsar=1,fps=12", filters)
        self.assertIn("eq=contrast=", filters)
        self.assertTrue(filters.endswith("format=nv12,hwupload"))
        self.assertIn("Done:", output)


if __name__ == "__main__":
    unittest.main()
