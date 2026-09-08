import io
import json
import socket
import sqlite3
import tarfile
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from app.align import align, detect_offset, similarity
from app.bilingual_catalog import CATALOG_FILENAME, build_catalog, ensure_catalog
from app.bulk_import import content_signature, scan_folder
from app.db import SCHEMA_VERSION, connect, insert_mapping
from app.importer import (
    archive_is_sparse_partial,
    fingerprint,
    image_features,
    images,
    infer_language,
    infer_metadata,
    mokuro_images,
    mokuro_page,
    page_id,
    safe_extract,
)
from app.mapping_import import normalize_data, normalize_record
from app.server import _create_server, choose_bulk_folder, choose_manga_source, open_reader
from app.converter import _native_command, run_mokuro


def make_page(path: Path, seed: int = 0, width: int = 300, height: int = 420) -> None:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + seed, 25, 270, 180 + seed), outline="black", width=8)
    draw.ellipse((45, 210 - seed, 250 - seed, 385), outline="black", width=10)
    draw.line((0, 80 + seed * 3, width, 330 - seed * 2), fill="black", width=7)
    image.save(path)


class AlignmentTests(unittest.TestCase):
    def test_monotonic_alignment_keeps_order_with_deleted_front_matter(self):
        got = align(["a", "cover", "b", "c"], ["a", "b", "c"])
        pairs = [(item.jp, item.en) for item in got if item.jp and item.en]
        self.assertEqual(pairs, [([0], [0]), ([2], [1]), ([3], [2])])

    def test_structural_offset_is_detected(self):
        hashes = [format(index * 7919, "0256b")[-256:] for index in range(6)]
        self.assertEqual(detect_offset(hashes, ["0" * 256, "1" * 256] + hashes)[0], 2)

    def test_spread_alignment_supports_one_to_two(self):
        combined = {
            "fingerprint": "01" * 128,
            "fingerprint_left": "0" * 256,
            "fingerprint_right": "1" * 256,
        }
        got = align([combined], [{"fingerprint": "0" * 256}, {"fingerprint": "1" * 256}])
        self.assertEqual((got[0].jp, got[0].en), ([0], [0, 1]))
        self.assertGreater(got[0].confidence, 0.95)

    def test_ambiguous_similarity_is_not_high_confidence(self):
        self.assertLess(similarity("01" * 128, "0011" * 64), 0.8)


class ImporterTests(unittest.TestCase):
    def test_zip_slip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.cbz"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.jpg", b"x")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                safe_extract(archive, Path(directory) / "out")

    def test_damaged_archive_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "broken.cbz"
            archive.write_bytes(b"not a zip")
            with self.assertRaisesRegex(ValueError, "Damaged"):
                safe_extract(archive, Path(directory) / "out")

    def test_tar_extracts_only_reader_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "001.png"
            make_page(page)
            archive = root / "volume.tar"
            mokuro = json.dumps({"title": "TAR Manga", "pages": []}).encode()
            with tarfile.open(archive, "w") as bundle:
                bundle.add(page, arcname="volume/001.png")
                info = tarfile.TarInfo("volume/book.mokuro")
                info.size = len(mokuro)
                bundle.addfile(info, io.BytesIO(mokuro))
                ignored = b"not a reader asset"
                info = tarfile.TarInfo("volume/notes.txt")
                info.size = len(ignored)
                bundle.addfile(info, io.BytesIO(ignored))
            target = root / "out"
            safe_extract(archive, target)
            self.assertTrue((target / "volume" / "001.png").is_file())
            self.assertTrue((target / "volume" / "book.mokuro").is_file())
            self.assertFalse((target / "volume" / "notes.txt").exists())

    def test_tar_traversal_and_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traversal = root / "traversal.tar"
            with tarfile.open(traversal, "w") as bundle:
                info = tarfile.TarInfo("../escape.jpg")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                safe_extract(traversal, root / "out-traversal")

            linked = root / "linked.tar"
            with tarfile.open(linked, "w") as bundle:
                info = tarfile.TarInfo("volume/page.jpg")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../escape.jpg"
                bundle.addfile(info)
            with self.assertRaisesRegex(ValueError, "unsafe entry"):
                safe_extract(linked, root / "out-linked")

    def test_damaged_tar_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "broken.tar"
            archive.write_bytes(b"not a tar archive")
            with self.assertRaisesRegex(ValueError, "Damaged"):
                safe_extract(archive, Path(directory) / "out")

    def test_empty_or_partial_tar_suggests_a_fresh_download(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "partial.tar"
            archive.write_bytes(b"\0" * 4096)
            with self.assertRaisesRegex(ValueError, "incomplete; download it again"):
                safe_extract(archive, Path(directory) / "out")

    def test_natural_sort_and_unicode_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "日本語 第1巻"
            root.mkdir()
            for name in ("10.png", "2.png", "１.png"):
                make_page(root / name)
            self.assertEqual([path.name for path in images(root)], ["１.png", "2.png", "10.png"])

    def test_metadata_inference_is_conservative(self):
        self.assertEqual(infer_metadata("Yotsuba - Volume 03")["series"], "Yotsuba")
        self.assertEqual(infer_metadata("Yotsuba - Volume 03")["volume"], "Volume 03")
        self.assertEqual(infer_metadata("よつばと！")["series"], "よつばと！")
        self.assertEqual(infer_metadata("よつばと！ 第12巻")["volume"], "Volume 12")
        self.assertEqual(infer_metadata("Series v03 ch12") ["chapter"], "Chapter 12")
        self.assertEqual(infer_metadata("Series v03 ch12") ["series"], "Series")
        self.assertEqual(infer_language("Series Vol 2 [English]"), "en")
        self.assertEqual(infer_language("Series Vol 2"), "jp")

    def test_current_mokuro_page_lookup_preserves_vertical_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "001.jpg"
            image.write_bytes(b"fixture")
            mokuro = root / "v.mokuro"
            mokuro.write_text(
                json.dumps(
                    {
                        "pages": [
                            {
                                "img_path": "001.jpg",
                                "img_width": 100,
                                "img_height": 200,
                                "blocks": [
                                    {
                                        "box": [1, 2, 30, 40],
                                        "vertical": True,
                                        "font_size": 18,
                                        "lines": ["日本語"],
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            got = mokuro_page(mokuro, image, root)
            self.assertEqual(got["blocks"][0]["lines"], ["日本語"])
            self.assertTrue(got["blocks"][0]["vertical"])

    def test_mokuro_image_order_is_exact_and_unsafe_paths_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "001.png"
            second = root / "002.png"
            make_page(first, 1)
            make_page(second, 2)
            outside = root.parent / "outside.png"
            outside.write_bytes(b"not part of the edition")
            sidecar = root / "book.mokuro"
            sidecar.write_text(
                json.dumps(
                    {
                        "pages": [
                            {"img_path": "002.png"},
                            {"img_path": "../outside.png"},
                            {"img_path": "001.png"},
                            {"img_path": "002.png"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(mokuro_images(sidecar), [second.resolve(), first.resolve()])
            outside.unlink()

    def test_visual_features_include_spread_halves(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "page.png"
            make_page(page)
            features = image_features(page)
            self.assertEqual(len(features["fingerprint"]), 256)
            self.assertEqual(len(features["fingerprint_left"]), 256)
            self.assertEqual(len(fingerprint(page)), 256)

    def test_stable_page_id_uses_relative_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "第1話" / "001.png"
            page.parent.mkdir()
            volume = str(uuid.uuid4())
            self.assertEqual(page_id(volume, page, root), page_id(volume, page, root))


class BulkScannerTests(unittest.TestCase):
    def test_archive_signature_survives_rename_and_sparse_download_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "original.tar"
            archive.write_bytes(b"header" + b"manga-data" * 100_000 + b"trailer")
            before = content_signature(archive)
            renamed = root / "renamed.tar"
            archive.rename(renamed)
            self.assertEqual(content_signature(renamed), before)

        fake_path = type(
            "SparsePath",
            (),
            {"stat": lambda self: type("Stat", (), {"st_size": 20 * 1024**2, "st_blocks": 1024})()},
        )()
        self.assertTrue(archive_is_sparse_partial(fake_path))

    def test_scan_classifies_recognized_mokuro_review_damage_and_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root / "batch"
            batch.mkdir()

            mokuro_folder = batch / "日本語 Volume 01"
            mokuro_folder.mkdir()
            mokuro_pages = []
            for index in range(2):
                page = mokuro_folder / f"{index + 1:03d}.png"
                make_page(page, index)
                mokuro_pages.append({"img_path": page.name, "blocks": []})
            (mokuro_folder / "book.mokuro").write_text(
                json.dumps({"title": "日本語の本", "pages": mokuro_pages}, ensure_ascii=False),
                encoding="utf-8",
            )

            review_folder = batch / "Unlabeled Volume 02"
            review_folder.mkdir()
            for index in range(2):
                make_page(review_folder / f"{index + 1:03d}.png", index + 3)

            cid = "bafy" + "a" * 55
            recognized = batch / f"{cid}.tar"
            sources = root / "sources"
            sources.mkdir()
            with tarfile.open(recognized, "w") as bundle:
                for index in range(4):
                    page = sources / f"{index + 1:03d}.png"
                    make_page(page, index + 7)
                    bundle.add(page, arcname=f"pages/{page.name}")
            damaged = batch / f"bafy{'b' * 55}.tar"
            damaged.write_bytes(b"incomplete")
            with zipfile.ZipFile(batch / "ordinary-documents.zip", "w") as bundle:
                bundle.writestr("notes.txt", "not manga")

            catalog = root / CATALOG_FILENAME
            catalog.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "archives": {
                            cid: {
                                "series": "Known Series",
                                "title": "Known Series · Volume 01",
                                "volume_label": "Volume 01",
                                "language": "en",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            progress = []
            items = scan_folder(batch, catalog, lambda done, total, name: progress.append((done, total, name)))
            self.assertEqual(len(items), 4)
            by_kind = {item["kind"]: [] for item in items}
            for item in items:
                by_kind[item["kind"]].append(item)
            known = next(item for item in items if item["path"] == str(recognized.resolve()))
            self.assertEqual((known["status"], known["language"], known["selected"]), ("ready", "en", True))
            self.assertTrue(known["recognized"])
            self.assertTrue(known["image_count_exact"])
            self.assertEqual(known["image_count"], 4)
            mokuro = next(item for item in items if item["kind"] == "mokuro")
            self.assertEqual((mokuro["status"], mokuro["language"], mokuro["image_count"]), ("ready", "jp", 2))
            review = next(item for item in items if item["kind"] == "image-folder")
            self.assertEqual((review["status"], review["language"], review["selected"]), ("ready", "jp", True))
            self.assertEqual(review["content_type"], "raw")
            broken = next(item for item in items if item["path"] == str(damaged.resolve()))
            self.assertEqual(broken["status"], "error")
            self.assertIn("Damaged", broken["error"])
            self.assertTrue(progress)
            self.assertEqual(progress[-1][0], progress[-1][1])


class DatabaseTests(unittest.TestCase):
    def test_schema_migrates_without_destroying_existing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            old = sqlite3.connect(path)
            old.executescript(
                """CREATE TABLE volumes(id TEXT PRIMARY KEY,title TEXT,series TEXT,language TEXT,source_path TEXT,page_count INTEGER,cover_path TEXT,mokuro_path TEXT,created_at TEXT);
                CREATE TABLE pages(id TEXT PRIMARY KEY,volume_id TEXT,ordinal INTEGER,path TEXT,width INTEGER,height INTEGER,UNIQUE(volume_id,ordinal));
                INSERT INTO volumes VALUES('v','Title','Series','jp','/missing',1,NULL,NULL,CURRENT_TIMESTAMP);
                INSERT INTO pages VALUES('p','v',0,'/missing/p.png',NULL,NULL);"""
            )
            old.commit()
            old.close()
            database = connect(path)
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(database.execute("SELECT title FROM volumes").fetchone()[0], "Title")
            self.assertIn("fingerprint_right", [row[1] for row in database.execute("PRAGMA table_info(pages)")])
            volume_columns = [row[1] for row in database.execute("PRAGMA table_info(volumes)")]
            self.assertIn("origin_path", volume_columns)
            self.assertIn("import_signature", volume_columns)
            self.assertIn("bookmarks", [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")])
            database.close()

    def test_grouped_mapping_has_exact_lookup_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = connect(Path(directory) / "test.sqlite3")
            for side in ("jp", "en"):
                database.execute(
                    "INSERT INTO volumes(id,title,series,language,source_path,page_count) VALUES(?,?,?,?,?,1)",
                    (side, side, "Series", side, directory),
                )
                database.execute(
                    "INSERT INTO pages(id,volume_id,ordinal,path) VALUES(?,?,0,?)",
                    (f"{side}-page", side, f"/{side}.png"),
                )
            insert_mapping(database, "m", "Series", ["jp-page"], ["en-page"], "manual", 1, True)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM mapping_pages WHERE mapping_id='m'").fetchone()[0], 2
            )
            database.close()


class MappingImporterTests(unittest.TestCase):
    def test_generic_importer_preserves_grouped_relationships(self):
        got = normalize_record(
            {"title": "Demo", "mappings": [{"jp_pages": ["j1", "j2"], "en_pages": ["e1"]}]}
        )
        self.assertEqual(got[0]["jp_pages"], ["j1", "j2"])
        self.assertEqual(got[0]["en_pages"], ["e1"])

    def test_real_archived_img_data_shape_is_normalized(self):
        got = normalize_data(
            {"title": "Demo", "img_data": {"jp": {"0_17": "0_19.jpg"}, "en": {"0_19": "0_17.jpg"}}}
        )
        self.assertEqual(got[0]["jp_pages"], ["0_17"])
        self.assertEqual(got[0]["en_pages"], ["0_19"])
        self.assertEqual(got[0]["source"], "bilingual-manga")


class BilingualCatalogTests(unittest.TestCase):
    def test_fresh_install_caches_and_validates_upstream_recognition_metadata(self):
        cid = "bafybeidh6ejstwf2fku7z63szbj57tpsi7tvd6kpea5sas6e7c2rlflcvy"
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as bundle:
            bundle.writestr(
                "json/BM_data.manga_data.json",
                json.dumps(
                    [
                        {
                            "_id": {"$oid": "record-1"},
                            "jp_data": {"ch_jph": [f"{cid}/hash"], "ch_najp": ["Volume 01"]},
                            "en_data": {"ch_enh": [], "ch_naen": []},
                        }
                    ]
                ),
            )
            bundle.writestr(
                "json/BM_data.manga_metadata.json",
                json.dumps([{"manga_titles": [{"enid": "record-1", "entit": "Fresh Install"}]}]),
            )

        class Response(io.BytesIO):
            def __init__(self, raw: bytes):
                super().__init__(raw)
                self.headers = {"Content-Length": str(len(raw))}

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.bilingual_catalog.urllib.request.urlopen",
            return_value=Response(payload.getvalue()),
        ):
            destination = Path(directory) / CATALOG_FILENAME
            result = ensure_catalog(destination)
            self.assertTrue(result["downloaded"])
            self.assertEqual(result["archives"], 1)
            self.assertEqual(json.loads(destination.read_text())["archives"][cid]["series"], "Fresh Install")
            cached = ensure_catalog(destination)
            self.assertFalse(cached["downloaded"])

    def test_cid_archive_is_recognized_without_bundling_catalogue_data(self):
        cid = "bafybeidh6ejstwf2fku7z63szbj57tpsi7tvd6kpea5sas6e7c2rlflcvy"
        catalog = build_catalog(
            [
                {
                    "_id": {"$oid": "record-1"},
                    "jp_data": {"ch_jph": [], "ch_najp": []},
                    "en_data": {"ch_enh": [f"{cid}/hash/%@rep@%"], "ch_naen": ["Volume 03"]},
                }
            ],
            [{"manga_titles": [{"enid": "record-1", "entit": "Example Series"}]}],
        )
        self.assertEqual(
            catalog["archives"][cid],
            {
                "series": "Example Series",
                "language": "en",
                "volume_label": "Volume 03",
                "title": "Example Series · Volume 03",
            },
        )


class BrowserLaunchTests(unittest.TestCase):
    def test_rapid_restart_waits_for_stable_port_to_be_released(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        release = threading.Timer(0.2, blocker.close)
        release.start()
        server = None
        try:
            server = _create_server(port)
            self.assertEqual(server.server_address[1], port)
        finally:
            if server:
                server.server_close()
            release.join()

    def test_converter_forces_arm64_for_universal_python_on_apple_silicon(self):
        with patch("app.converter.sys.platform", "darwin"), patch(
            "app.converter.platform.machine", return_value="arm64"
        ):
            self.assertEqual(
                _native_command(["/path/python", "-m", "mokuro"]),
                ["/usr/bin/arch", "-arm64", "/path/python", "-m", "mokuro"],
            )

    def test_native_bulk_picker_returns_folder_and_handles_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = type(
                "Completed", (), {"returncode": 0, "stdout": f"{directory}\n", "stderr": ""}
            )()
            cancelled = type(
                "Completed", (), {"returncode": 1, "stdout": "", "stderr": "execution error: User canceled. (-128)"}
            )()
            with (
                patch("app.server.platform.system", return_value="Darwin"),
                patch("app.server.subprocess.run", return_value=selected) as run,
            ):
                self.assertEqual(choose_bulk_folder(), Path(directory).resolve())
                arguments = run.call_args.args[0]
                self.assertEqual(arguments[:3], ["osascript", "-l", "JavaScript"])
                self.assertIn("NSOpenPanel", arguments[-1])
                self.assertIn("activateIgnoringOtherApps", arguments[-1])
            with (
                patch("app.server.platform.system", return_value="Darwin"),
                patch("app.server.subprocess.run", return_value=cancelled),
            ):
                self.assertIsNone(choose_bulk_folder())

    def test_windows_manga_picker_uses_native_powershell_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = type(
                "Completed", (), {"returncode": 0, "stdout": f"{directory}\n", "stderr": ""}
            )()
            with (
                patch("app.server.platform.system", return_value="Windows"),
                patch("app.server.subprocess.run", return_value=selected) as run,
            ):
                self.assertEqual(choose_manga_source(), Path(directory).resolve())
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[:3], ["powershell.exe", "-NoProfile", "-STA"])
            self.assertIn("FolderBrowserDialog", arguments[-1])

    def test_mokuro_uses_automatic_hardware_acceleration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pages = root / "pages"
            pages.mkdir()

            def fake_stream(command, _environment, _progress, _phase):
                (root / "pages.mokuro").write_text('{"pages": []}', encoding="utf-8")
                self.assertNotIn("--force_cpu", command)
                self.assertIn("--legacy_html=False", command)

            with patch("app.converter._run_stream", side_effect=fake_stream):
                result = run_mokuro(Path("python"), pages, root, lambda *_: None)
            self.assertEqual(result, root / "pages.mokuro")

    def test_macos_uses_launchservices_for_an_existing_chrome_profile(self):
        completed = type("Completed", (), {"returncode": 0})()
        with (
            patch("app.server.platform.system", return_value="Darwin"),
            patch("app.server.Path.exists", return_value=True),
            patch("app.server.subprocess.run", return_value=completed) as run,
            patch("app.server.subprocess.Popen") as popen,
        ):
            self.assertTrue(open_reader("http://127.0.0.1:12345"))

        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["open", "-a", "Google Chrome", "http://127.0.0.1:12345"],
        )
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
