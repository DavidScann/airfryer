import contextlib
import io
import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airfryer import cli


class ReviewRegressionTests(unittest.TestCase):
    def test_auto_codec_uses_h264_gpu_when_hevc_is_unsupported(self):
        gpu = cli._Encoder("h264_vulkan", True, "Vulkan", None, 0)
        with patch.object(cli, "probe_vulkan", side_effect=[(None, "HEVC unsupported"), (gpu, None)]):
            self.assertIs(cli.choose_encoder("auto", "auto", "ffmpeg"), gpu)

    def test_auto_codec_uses_av1_gpu_before_falling_back_to_software(self):
        gpu = cli._Encoder("av1_vulkan", True, "Vulkan", None, 1)
        with patch.object(cli, "probe_vulkan", side_effect=[
                (None, "HEVC unsupported"), (None, "H264 unsupported"), (gpu, None)]):
            self.assertIs(cli.choose_encoder("auto", "auto", "ffmpeg"), gpu)

    def test_auto_codec_falls_back_after_all_gpu_codecs_fail(self):
        with patch.object(cli, "probe_vulkan", return_value=(None, "unsupported")):
            encoder = cli.choose_encoder("auto", "auto", "ffmpeg")
        self.assertEqual(encoder.name, "libx265")
        self.assertIn("single-pass", encoder.mode_text)

    def test_vulkan_colour_filters_transfer_frames_to_cpu_and_back(self):
        filters = cli._vulkan_filter(cli.FRY_FILTERS, True).split(",")
        self.assertIn("hwdownload", filters)
        self.assertIn("hwupload", filters)
        colour = next(i for i, value in enumerate(filters) if value.startswith("eq="))
        self.assertLess(filters.index("hwdownload"), colour)
        self.assertGreater(filters.index("hwupload"), colour)

    def test_vulkan_keeps_downscale_and_upscale_on_the_gpu(self):
        filters = cli._vulkan_filter([
            "scale=66:50:flags=neighbor", "scale=320:240:flags=neighbor", "setsar=1"
        ], True).split(",")
        self.assertEqual(filters[:2], [
            "scale_vulkan=format=nv12:w=66:h=50:scaler=nearest",
            "scale_vulkan=format=nv12:w=320:h=240:scaler=nearest",
        ])
        self.assertGreater(filters.index("hwdownload"), 1)

    def test_vulkan_compression_runs_on_cpu_between_gpu_scaling_steps(self):
        filters = cli._vulkan_filter([
            "scale=160:120:flags=bilinear", "format=yuv420p", "spp=quality=1:qp=63:mode=hard",
            "scale=320:240:flags=bilinear", "gblur=sigma=0.6"
        ], True).split(",")
        compression = filters.index("spp=quality=1:qp=63:mode=hard")
        enlargement = next((i for i, value in enumerate(filters)
                            if value.startswith("scale_vulkan=") and ":w=320:h=240" in value), None)
        self.assertIsNotNone(enlargement, "Enlargement must remain on the GPU after CPU compression")
        self.assertLess(compression, enlargement)
        self.assertIn("hwdownload", filters[:compression])
        self.assertIn("hwupload", filters[compression:enlargement])
        blur = filters.index("gblur=sigma=0.6")
        self.assertIn("hwdownload", filters[enlargement:blur])
        self.assertTrue(filters[-1] == "hwupload")

    def test_new_directory_destination_keeps_upstream_naming_behaviour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            container, output, parent = cli.resolve_destination(str(root / "source.mov"), str(root / "new-output"))
            self.assertEqual(container, "mp4")
            self.assertEqual(Path(output), root / "new-output" / "source.fry.mp4")
            self.assertEqual(Path(parent), root / "new-output")
            self.assertFalse((root / "new-output").exists())

    def test_run_activates_vulkan_decode_only_after_a_successful_probe(self):
        for supported in (True, False):
            with self.subTest(supported=supported), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.mp4"
                source.write_bytes(b"source")
                gpu = cli._Encoder("hevc_vulkan", True, "Vulkan", None, 0)
                commands = []

                def encode(command, verbose):
                    commands.append(command)
                    Path(command[-1]).write_bytes(b"encoded")
                    return 0, ""

                with patch.object(cli, "resolve_tool", side_effect=lambda explicit, name: name), \
                        patch.object(cli, "choose_encoder", return_value=gpu), \
                        patch.object(cli, "probe_vulkan_decode", return_value=(supported, None if supported else "unsupported")), \
                        patch.object(cli, "_run_encode", side_effect=encode), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.run(str(source), str(root / "output.mp4")), 0)
                self.assertEqual("-hwaccel" in commands[0], supported)
                self.assertGreater(commands[0].index("-c:v"), commands[0].index("-i"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg integration tools unavailable")
class FFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=True).stdout
        if "libx265" not in encoders or "libmp3lame" not in encoders:
            raise unittest.SkipTest("FFmpeg needs libx265 and libmp3lame for integration tests")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airfryer-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _fry(self, audio):
        source = self.root / "source.mkv"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=4"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100:duration=4"]
        command += ["-vf", "setsar=8/7", "-c:v", "ffv1"]
        if audio:
            command += ["-c:a", "pcm_s16le"]
        command += [str(source)]
        subprocess.run(command, check=True, capture_output=True)
        destination = self.root / "fried.mp4"
        with patch.object(cli, "probe_vulkan", return_value=(None, "No test GPU")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main([str(source), str(destination)])
        self.assertEqual(code, 0, output.getvalue())
        data = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(destination)],
                              check=True, capture_output=True, text=True)
        return destination, json.loads(data.stdout)["streams"]

    def test_real_encode_has_fixed_geometry_fps_and_distorted_32k_mp3(self):
        destination, streams = self._fry(True)
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (320, 240))
        self.assertEqual(video["sample_aspect_ratio"], "1:1")
        self.assertEqual(video["display_aspect_ratio"], "4:3")
        self.assertEqual(video["avg_frame_rate"], "12/1")
        self.assertEqual(video["color_range"], "tv")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "mp3")
        # MP4 duration includes MP3 encoder delay, so ffprobe's average differs
        # slightly from the fixed 32 kbps frame bitrate on short clips.
        self.assertAlmostEqual(int(audio["bit_rate"]), 32000, delta=1600)

        decoded = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(destination),
                                  "-t", "1", "-vn", "-ac", "1", "-ar", "24000", "-f", "f32le", "-"],
                                 check=True, capture_output=True).stdout
        samples = struct.unpack("<" + "f" * (len(decoded) // 4), decoded)

        def amplitude(frequency):
            real = sum(sample * math.cos(2 * math.pi * frequency * index / 24000) for index, sample in enumerate(samples))
            imaginary = sum(sample * math.sin(2 * math.pi * frequency * index / 24000) for index, sample in enumerate(samples))
            return math.hypot(real, imaginary)

        self.assertGreater(amplitude(3000) / amplitude(1000), 0.05, "Gain alone does not distort floating-point audio")
        analysis = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(destination), "-vn", "-af",
                                   "loudnorm=I=-16:TP=-2:print_format=json", "-f", "null", "-"],
                                  check=True, capture_output=True, text=True).stderr
        measured, _ = json.JSONDecoder().raw_decode(analysis[analysis.rfind("{"):])
        self.assertLessEqual(float(measured["input_i"]), -14.5)

    def test_real_video_only_source_encodes_without_adding_audio(self):
        _, streams = self._fry(False)
        self.assertEqual([stream["codec_type"] for stream in streams], ["video"])
        self.assertEqual(streams[0]["avg_frame_rate"], "12/1")

    def test_colour_recipe_keeps_luma_within_limited_range(self):
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                 "nullsrc=s=320x240,geq=lum='16+219*X/W':cb=128:cr=128",
                                 "-vf", ",".join(cli.FRY_FILTERS), "-frames:v", "1", "-pix_fmt", "yuv420p",
                                 "-f", "rawvideo", "-"], check=True, capture_output=True)
        luma = result.stdout[:320 * 240]
        self.assertGreaterEqual(min(luma), 16)
        self.assertLessEqual(max(luma), 235)

    def test_blurry_recipe_softens_edges_without_a_hard_pixel_grid(self):
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                 "nullsrc=s=640x480,geq=lum='if(lt(X,W/2),32,224)':cb=128:cr=128",
                                 "-vf", ",".join(cli.FRY_FILTERS), "-frames:v", "1", "-pix_fmt", "yuv420p",
                                 "-f", "rawvideo", "-"], check=True, capture_output=True)
        luma = result.stdout[:320 * 240]
        self.assertEqual(len(luma), 320 * 240)
        row = luma[120 * 320:121 * 320]
        self.assertLess(max(abs(first - second) for first, second in zip(row, row[1:])), 100,
                        "Blurred edges must spread the transition across several pixels")
        self.assertGreater(len(set(row)), 6, "Smooth enlargement must create blended tones along an edge")


if __name__ == "__main__":
    unittest.main()
