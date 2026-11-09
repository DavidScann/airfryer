"""Independent reviewer tests for pressurecooker.cli.

These tests are complementary to test_cli.py; they exercise paths that the
existing suite does not cover: WebM/container policy, probe fallbacks,
preset and resolution mappings, audio budget boundaries, the FFmpeg
argument list for two-pass and single-pass encoders, and the publish
path. Only the standard library is used.
"""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pressurecooker import cli


def _unescape_x265_stats(text):
    """Undo the escaping applied in cli._pass_args for pass=N:stats=<path>."""
    # cli.py:609 inserts a backslash before every \\, :, or ' in the prefix.
    return re.sub(r"\\([\\:'])", r"\1", text)


def _is_analysis_pass(command):
    """Return True when the FFmpeg argv is a pass-1 analysis (writes to os.devnull)."""
    return command[-1] == os.devnull


def _capture_stdout(function, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = function(*args, **kwargs)
    return result, buffer.getvalue()


class DestinationTests(unittest.TestCase):
    """Container/extension policy and directory-vs-file detection."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pressurecooker-review-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.mp4"
        self.source.write_bytes(b"input")

    def options(self, destination, **kwargs):
        opts = cli.Options(source=str(self.source), destination=str(destination),
                           target_mib=kwargs.pop("target_mib", 1))
        for key, value in kwargs.items():
            setattr(opts, key, value)
        return opts

    def test_container_flag_conflicts_with_destination_extension(self):
        opts = self.options(self.root / "out.mp4", container="mkv")
        with self.assertRaises(cli.CliError) as caught:
            cli.resolve_destination(opts, str(self.source))
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("conflicts with --container", str(caught.exception))

    def test_container_flag_matching_extension_is_accepted(self):
        opts = self.options(self.root / "out.mkv", container="mkv")
        container, out_file, _ = cli.resolve_destination(opts, str(self.source))
        self.assertEqual(container, "mkv")
        self.assertEqual(Path(out_file).name, "out.mkv")

    def test_unrecognised_extension_is_treated_as_directory(self):
        opts = self.options(self.root / "out", container="mkv")
        container, out_file, out_dir = cli.resolve_destination(opts, str(self.source))
        self.assertEqual(container, "mkv")
        self.assertEqual(out_dir, str(self.root / "out"))
        self.assertTrue(Path(out_file).name.endswith(".fit1.mkv"), out_file)

    def test_destination_ending_in_separator_is_directory(self):
        (self.root / "subdir").mkdir()
        opts = self.options(str(self.root / "subdir") + os.sep)
        _, out_file, out_dir = cli.resolve_destination(opts, str(self.source))
        self.assertEqual(out_dir, str(self.root / "subdir"))
        self.assertEqual(Path(out_file).name, "input.fit1.mp4")

    def test_directory_destination_uses_source_stem_for_autoname(self):
        opts = self.options(str(self.root))
        _, out_file, _ = cli.resolve_destination(opts, str(self.source))
        self.assertEqual(Path(out_file).name, "input.fit1.mp4")


class _FakeMediaMixin:
    """Shared harness that runs cli.run against a fake ffmpeg.

    The mock records every command, produces the two-pass statistics file the
    real encoder would write, and finishes the encode with a small output.
    """

    def make_media(self, suffix=".mp4"):
        self.temporary = tempfile.TemporaryDirectory(prefix="pressurecooker-review-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / ("video" + suffix)
        self.source.write_bytes(b"v")
        self.output = self.root / ("out" + suffix)

    def options(self, **overrides):
        opts = cli.Options(source=str(self.source), destination=str(self.output),
                           target_mib=10, codec="hevc", hw="off", audio_kbps=128)
        for key, value in overrides.items():
            setattr(opts, key, value)
        return opts

    def record_encode(self, stats_prefix, commands, fail=False):
        def fake_run(cmd, verbose):
            commands.append(list(cmd))
            if fail:
                return 1, "boom"
            if _is_analysis_pass(cmd):
                # Analysis pass: emit the stats file the encoder would.
                if encoder_name(cmd) == "libx265":
                    Path(stats_prefix).write_bytes(b"stats")
                else:
                    Path(stats_prefix + "-0.log").write_bytes(b"stats")
            else:
                Path(cmd[-1]).write_bytes(b"finished")
            return 0, ""
        return fake_run


def encoder_name(cmd):
    for index, arg in enumerate(cmd):
        if arg == "-c:v":
            return cmd[index + 1]
    return None


class AudioPolicyTests(_FakeMediaMixin, unittest.TestCase):
    """Boundary conditions for the copy-vs-reencode rule at cli.py:682."""

    def setUp(self):
        self.make_media()

    def plan(self, audio, codec="hevc", container="mp4", **overrides):
        destination = self.root / ("out." + container)
        opts = self.options(codec=codec, container=container, destination=str(destination),
                            overwrite=True, **overrides)
        if destination.exists():
            destination.unlink()
        encoder = cli._Encoder(cli.SOFTWARE_ENCODERS[codec], False, "software", None)
        commands = []
        with patch.object(cli, "_new_log_prefix",
                          return_value=str(self.root / ".fake-stats")) as stub, \
             patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=audio), \
             patch.object(cli, "probe_dimensions", return_value=(1920, 1080)), \
             patch.object(cli, "_run_encode",
                          side_effect=self.record_encode(stub.return_value, commands)), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.run(opts)
        return commands

    def test_aac_at_cap_boundary_is_copied(self):
        commands = self.plan(cli._Audio("aac", cli.AUDIO_CAP_KBPS))
        for command in commands:
            if "-c:a" in command:
                self.assertEqual(command[command.index("-c:a") + 1], "copy", command)

    def test_aac_just_above_cap_is_reencoded_at_audio_kbps(self):
        commands = self.plan(cli._Audio("aac", cli.AUDIO_CAP_KBPS + 1))
        final = commands[-1]
        self.assertEqual(final[final.index("-c:a") + 1], "aac")
        # 128 kbps fits under floor(0.15 * total) for this duration/target.
        self.assertIn("-b:a", final)
        self.assertEqual(final[final.index("-b:a") + 1], "128k")

    def test_opus_in_webm_under_cap_is_copied(self):
        commands = self.plan(cli._Audio("opus", 96), codec="av1", container="webm")
        for command in commands:
            if "-c:a" in command:
                self.assertEqual(command[command.index("-c:a") + 1], "copy", command)

    def test_aac_in_webm_is_reencoded_as_libopus(self):
        commands = self.plan(cli._Audio("aac", 128), codec="av1", container="webm")
        final = commands[-1]
        self.assertEqual(final[final.index("-c:a") + 1], "libopus")
        self.assertIn("-vbr", final)
        self.assertIn("constrained", final)

    def test_video_only_source_emits_no_audio_stream(self):
        commands = self.plan(None)
        for command in commands:
            self.assertNotIn("-c:a", command)
            self.assertNotIn("0:a:0", command)

    def test_heavier_audio_budget_leaves_less_video(self):
        loud = self.plan(cli._Audio("aac", 200), audio_kbps=192)
        quiet = self.plan(cli._Audio("aac", 60))  # under cap -> copy at 60 kbps

        def video_kbps(command):
            for index, arg in enumerate(command):
                if arg == "-b:v":
                    return int(command[index + 1].rstrip("k"))
            return None

        self.assertLess(video_kbps(loud[-1]), video_kbps(quiet[-1]))


class ProbeFallbackTests(unittest.TestCase):
    """Duration and audio probing take ffprobe output verbatim, in order."""

    def test_probe_duration_falls_back_to_stream_when_format_missing(self):
        calls = []

        def fake(cmd):
            calls.append(cmd)
            if "format=duration" in cmd:
                return 0, "N/A\n", ""
            return 0, "12.5\n", ""

        with patch.object(cli, "_capture", side_effect=fake):
            duration = cli.probe_duration("ffprobe", "input.mp4")
        self.assertEqual(duration, 12.5)
        self.assertEqual(len(calls), 2, "second attempt should run")

    def test_probe_duration_rejects_when_both_attempts_fail(self):
        with patch.object(cli, "_capture", return_value=(0, "N/A\n", "")), \
             self.assertRaises(cli.CliError) as caught:
            cli.probe_duration("ffprobe", "input.mp4")
        self.assertEqual(caught.exception.code, 2)

    def test_probe_duration_accepts_first_finite_positive_line(self):
        with patch.object(cli, "_capture", return_value=(0, "0\n5.0\nabc\n", "")):
            self.assertEqual(cli.probe_duration("ffprobe", "input.mp4"), 5.0)

    def test_probe_audio_uses_bps_tag_when_stream_bit_rate_missing(self):
        payload = json.dumps({"streams": [{"codec_name": "aac",
                                             "tags": {"BPS-eng": "128000"}}]})
        with patch.object(cli, "_capture", return_value=(0, payload, "")):
            audio = cli.probe_audio("ffprobe", "input.mp4")
        self.assertEqual(audio.codec, "aac")
        self.assertAlmostEqual(audio.kbps, 128.0)

    def test_probe_audio_prefers_stream_bit_rate_over_tag(self):
        payload = json.dumps({"streams": [{"codec_name": "aac", "bit_rate": "96000",
                                            "tags": {"BPS": "256000"}}]})
        with patch.object(cli, "_capture", return_value=(0, payload, "")):
            audio = cli.probe_audio("ffprobe", "input.mp4")
        self.assertAlmostEqual(audio.kbps, 96.0)


class PresetMappingTests(unittest.TestCase):
    """Named and numeric presets map as the README documents, not as the tables repeat."""

    def test_qsv_named_extremes_collapse_to_supported_edges(self):
        self.assertEqual(cli.resolve_preset("ultrafast", "h264", True), "veryfast")
        self.assertEqual(cli.resolve_preset("placebo", "h264", True), "veryslow")
        self.assertEqual(cli.resolve_preset("superfast", "hevc", True), "veryfast")

    def test_qsv_numeric_presets_span_the_supported_range(self):
        # Numeric presets are SVT-AV1 style; QSV collapses them onto the named scale.
        self.assertEqual(cli.resolve_preset("0", "h264", True), "veryslow")
        self.assertEqual(cli.resolve_preset("7", "h264", True), "faster")
        # Values outside the table default to the fastest named QSV preset.
        self.assertEqual(cli.resolve_preset("13", "h264", True), "veryfast")

    def test_av1_named_presets_preserve_speed_order(self):
        values = [int(cli.resolve_preset(name, "av1", False)) for name in
                  ["ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow", "placebo"]]
        # SVT-AV1: lower number = higher quality. The list must be strictly decreasing.
        self.assertEqual(values, sorted(values, reverse=True))

    def test_av1_rejects_out_of_range_numeric_preset(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.resolve_preset("14", "av1", False)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("AV1", str(caught.exception))

    def test_software_h264_and_hevc_only_accept_named_presets(self):
        for codec in ("h264", "hevc"):
            with self.subTest(codec=codec), self.assertRaises(cli.CliError) as caught:
                cli.resolve_preset("5", codec, False)
            self.assertEqual(caught.exception.code, 2)

    def test_describe_preset_marks_only_mapped_values(self):
        self.assertIn("mapped from", cli.describe_preset("ultrafast", "veryfast", True))
        self.assertNotIn("mapped", cli.describe_preset("veryfast", "veryfast", True))
        self.assertIn("mapped from", cli.describe_preset("fast", "8", False))


class ResolutionTests(unittest.TestCase):
    def test_odd_dimensions_round_down_to_even(self):
        self.assertEqual(cli.parse_resolution("1081x1081"), (1080, 1080))

    def test_resolution_alias_pairs_are_consistent(self):
        for alias, exact in [("sd", "360p"), ("hd", "720p"), ("fullhd", "1080p"),
                             ("qhd", "1440p"), ("uhd", "2160p")]:
            with self.subTest(alias=alias):
                self.assertEqual(cli.parse_resolution(alias), cli.parse_resolution(exact))

    def test_resolution_rejects_oversize_and_zero(self):
        # parse_resolution rounds odd values down to even before checking the
        # cap, so only clearly oversize or zero cases are rejected here.
        for value in ("20000x8", "0x0", "16386x2", "0x720", "16384x16386"):
            with self.subTest(value=value), self.assertRaises(cli.CliError):
                cli.parse_resolution(value)

    def test_resolution_rejects_malformed_text(self):
        for value in ("abc", "640x", "x360", "640x360x1"):
            with self.subTest(value=value), self.assertRaises(cli.CliError):
                cli.parse_resolution(value)


class CommandArgumentTests(_FakeMediaMixin, unittest.TestCase):
    """The FFmpeg argv lists are constructed correctly, not shell-interpolated."""

    def setUp(self):
        self.make_media()

    def test_pass_args_for_libx265_uses_x265_params_and_escapes_specials(self):
        original = r"C:\tmp\pressurecooker:1'a"
        args = cli._pass_args("libx265", 1, original)
        self.assertEqual(args[0], "-x265-params")
        payload = args[1]
        self.assertTrue(payload.startswith("pass=1:stats="), payload)
        escaped = payload.split("stats=", 1)[1]
        self.assertNotEqual(escaped, original, "expected escaping of special chars")
        # Every reserved character is preceded by a backslash we inserted.
        self.assertEqual(_unescape_x265_stats(escaped), original)

    def test_pass_args_for_libx264_scopes_pass_flags_to_video_stream(self):
        args = cli._pass_args("libx264", 2, "/tmp/prefix")
        self.assertEqual(args, ["-pass:v", "2", "-passlogfile:v", "/tmp/prefix"])

    def test_format_command_quotes_windows_arguments_for_powershell(self):
        with patch.object(cli.os, "name", "nt"):
            rendered = cli._format_command(["ffmpeg", "-i", "a b.mp4"])
        self.assertTrue(rendered.startswith("& "), rendered)
        self.assertIn("'a b.mp4'", rendered)

    def test_format_command_uses_posix_shlex_when_not_windows(self):
        with patch.object(cli.os, "name", "posix"):
            rendered = cli._format_command(["ffmpeg", "-i", "a b.mp4"])
        self.assertEqual(rendered, "ffmpeg -i 'a b.mp4'")

    def test_qsv_encoder_single_command_has_no_pass_flags(self):
        opts = self.options(codec="hevc", hw="qsv", dry_run=True, target_mib=5)
        encoder = cli._Encoder("hevc_qsv", True, "QSV single-pass VBR", None)
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)):
            _, output = _capture_stdout(cli.run, opts)
        self.assertIn("single pass, QSV", output)
        self.assertNotIn("Pass 1 command", output)
        self.assertNotIn("-passlogfile", output)

    def test_software_encoder_dry_run_emits_two_pass_commands(self):
        opts = self.options(codec="h264", hw="off", dry_run=True, target_mib=5)
        encoder = cli._Encoder("libx264", False, "software", None)
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=cli._Audio("aac", 128)), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)):
            _, output = _capture_stdout(cli.run, opts)
        self.assertIn("Pass 1 command", output)
        self.assertIn("Pass 2 command", output)
        self.assertIn("-passlogfile", output)

    def test_libx265_uses_x265_params_not_passlogfile(self):
        opts = self.options(codec="hevc", hw="off", dry_run=True)
        encoder = cli._Encoder("libx265", False, "software", None)
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder", return_value=encoder), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)):
            _, output = _capture_stdout(cli.run, opts)
        self.assertIn("-x265-params", output)
        self.assertNotIn("-passlogfile", output)


class TargetTooSmallTests(_FakeMediaMixin, unittest.TestCase):
    """Duration times target drives a hard bitrate floor; below that, exit 4."""

    def setUp(self):
        self.make_media()

    def test_long_duration_and_tiny_target_exits_4(self):
        argv = [str(self.source), str(self.output),
                "-t", "0.001", "-c", "h264", "--hw", "off"]
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "probe_duration", return_value=3600), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)), \
             patch.object(cli, "_run_encode", side_effect=AssertionError):
            _, output = _capture_stdout(cli.main, argv)
        self.assertIn("Target too small", output)

    def test_video_only_1mib_over_60s_still_encodes(self):
        # 1 MiB / 60 s gives ~139.8 kbps total; after the 0.97 mux margin that
        # leaves ~135 kbps for video, above the 50 kbps floor.
        opts = self.options(codec="h264", hw="off", target_mib=1)
        commands = []
        with patch.object(cli, "_new_log_prefix",
                          return_value=str(self.root / ".fake-stats")) as stub, \
             patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder",
                          return_value=cli._Encoder("libx264", False, "software", None)), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)), \
             patch.object(cli, "_run_encode",
                          side_effect=self.record_encode(stub.return_value, commands)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.run(opts)
        self.assertEqual(code, 0)
        self.assertTrue(self.output.is_file())
        self.assertEqual(self.output.read_bytes(), b"finished")


class PublishPathTests(_FakeMediaMixin, unittest.TestCase):
    """Final publication uses replace when --overwrite, rename otherwise."""

    def setUp(self):
        self.make_media()

    def _run(self, overwrite, publish_override=None):
        opts = self.options(codec="h264", hw="off", overwrite=overwrite)
        commands = []
        calls = {}

        def make_publish(name):
            def publish(src, dst):
                calls[name] = (src, dst)
                # Actually move the file so later code sees it.
                Path(dst).write_bytes(Path(src).read_bytes())
                Path(src).unlink()
            return publish

        stats_prefix = str(self.root / ".fake-stats")

        def fake_run(cmd, verbose):
            commands.append(list(cmd))
            if _is_analysis_pass(cmd):
                Path(stats_prefix + "-0.log").write_bytes(b"stats")
            else:
                Path(cmd[-1]).write_bytes(b"finished")
            return 0, ""

        if publish_override is not None:
            publisher = publish_override
        else:
            publisher = make_publish("publish")

        replace_cm = patch.object(cli.os, "replace", side_effect=publisher)
        rename_cm = patch.object(cli.os, "rename", side_effect=publisher)
        link_cm = patch.object(cli.os, "link", side_effect=publisher)
        unlink_cm = patch.object(cli.os, "unlink", wraps=os.unlink)
        with patch.object(cli, "_new_log_prefix", return_value=stats_prefix), \
             patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "choose_encoder",
                          return_value=cli._Encoder("libx264", False, "software", None)), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)), \
             patch.object(cli, "_run_encode", side_effect=fake_run), \
             replace_cm, rename_cm, link_cm, unlink_cm, \
             contextlib.redirect_stdout(io.StringIO()):
            if overwrite:
                self.output.write_bytes(b"previous")
            cli.main([str(self.source), str(self.output),
                      "-t", "1", "-c", "h264", "--hw", "off"]
                     + (["--overwrite"] if overwrite else []))
        return calls

    def test_overwrite_publishes_via_os_replace(self):
        calls = self._run(overwrite=True)
        self.assertIn("publish", calls)
        src, dst = calls["publish"]
        self.assertTrue(Path(src).name.startswith(".pressurecooker-"))
        self.assertEqual(Path(dst), self.output)

    def test_no_overwrite_publishes_via_rename_or_link(self):
        calls = self._run(overwrite=False)
        # On POSIX the code uses os.link; on Windows it uses os.rename.
        self.assertIn("publish", calls)


class ArgumentParsingTests(unittest.TestCase):
    """Flag handling for the paths and shapes that real users hit."""

    def test_double_dash_stops_option_parsing(self):
        opts = cli.parse_args(["-t", "5", "--", "-weird.mp4", "out.mp4"])
        self.assertEqual(opts.source, "-weird.mp4")
        self.assertEqual(opts.destination, "out.mp4")
        self.assertEqual(opts.target_mib, 5.0)

    def test_inline_equals_forms_are_accepted(self):
        opts = cli.parse_args(["--target-mb=10", "--codec=h264", "in.mp4", "out.mp4"])
        self.assertEqual(opts.target_mib, 10.0)
        self.assertEqual(opts.codec, "h264")

    def test_missing_value_for_option_is_usage_error(self):
        for argv in (["in.mp4", "out.mp4", "-t"], ["in.mp4", "out.mp4", "--target-mb="]):
            with self.subTest(argv=argv), self.assertRaises(cli.CliError) as caught:
                cli.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_extra_positional_argument_is_rejected(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.parse_args(["a.mp4", "b.mp4", "c.mp4", "-t", "5"])
        self.assertEqual(caught.exception.code, 2)

    def test_case_insensitive_flag_recognition(self):
        opts = cli.parse_args(["in.mp4", "out.mp4", "--TARGET-MB", "7", "--Codec", "av1"])
        self.assertEqual(opts.target_mib, 7.0)
        self.assertEqual(opts.codec, "av1")

    def test_unknown_flag_reports_usage(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.parse_args(["a.mp4", "b.mp4", "-t", "5", "--nonsense"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Unknown option", str(caught.exception))


class InterruptCleanupTests(unittest.TestCase):
    """Ctrl-C returns 130 and removes the temporary output."""

    def test_keyboard_interrupt_returns_130_and_cleans_temp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pressurecooker-review-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "v.mp4"
        source.write_bytes(b"v")
        out = root / "o.mp4"

        def raise_interrupt(cmd, verbose):
            if _is_analysis_pass(cmd):
                # Emulate pass-1 producing stats so pass-2 runs and interrupts.
                pass
            else:
                Path(cmd[-1]).write_bytes(b"partial")
            raise KeyboardInterrupt

        argv = [str(source), str(out), "-t", "5", "--codec", "h264", "--hw", "off"]
        with patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)), \
             patch.object(cli, "_run_encode", side_effect=raise_interrupt):
            code = cli.main(argv)
        self.assertEqual(code, cli.EXIT_INTERRUPTED)
        self.assertFalse(out.exists())
        self.assertEqual(list(root.glob(".pressurecooker-*")), [])


class PublicationRaceTests(unittest.TestCase):
    """Publishing after an unexpected destination appearance reports exit 2."""

    def test_posix_link_collision_reports_destination_appeared(self):
        if os.name == "nt":
            self.skipTest("The os.link publish branch is POSIX only.")
        temporary = tempfile.TemporaryDirectory(prefix="pressurecooker-review-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "v.mp4"
        source.write_bytes(b"v")
        out = root / "o.mp4"
        stats_prefix = str(root / ".fake-stats")

        def fake_run(cmd, verbose):
            if _is_analysis_pass(cmd):
                Path(stats_prefix + "-0.log").write_bytes(b"stats")
            else:
                Path(cmd[-1]).write_bytes(b"finished")
            return 0, ""

        def colliding_link(src, dst):
            raise FileExistsError(17, "exists", dst)

        with patch.object(cli, "_new_log_prefix", return_value=stats_prefix), \
             patch.object(cli, "resolve_tool", side_effect=lambda value, name: name), \
             patch.object(cli, "probe_duration", return_value=60), \
             patch.object(cli, "probe_audio", return_value=None), \
             patch.object(cli, "probe_dimensions", return_value=(1280, 720)), \
             patch.object(cli, "_run_encode", side_effect=fake_run), \
             patch.object(cli.os, "link", side_effect=colliding_link), \
             patch.object(cli.os, "name", "posix"), \
             contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main([str(source), str(out), "-t", "5",
                             "--codec", "h264", "--hw", "off"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("Destination appeared during encoding", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
