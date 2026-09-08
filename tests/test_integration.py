import io
import http.client
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import app.server as server_module
from app.db import connect


def make_page(path: Path, seed: int) -> None:
    image = Image.new("RGB", (320, 460), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15 + seed, 20, 300, 180 + seed), outline="black", width=9)
    draw.ellipse((35, 205, 285 - seed, 420), outline="black", width=11)
    draw.line((0, 75 + seed * 4, 320, 360 - seed * 2), fill="black", width=8)
    image.save(path)


def make_edition(root: Path, name: str, mokuro: bool = False) -> Path:
    folder = root / name
    folder.mkdir()
    pages = []
    for index in range(3):
        page = folder / f"{index + 1:03d}.png"
        make_page(page, index * 7)
        pages.append(
            {
                "img_path": page.name,
                "img_width": 320,
                "img_height": 460,
                "blocks": [
                    {
                        "box": [40, 45, 100, 180],
                        "vertical": True,
                        "font_size": 18,
                        "lines_coords": [[[40, 45], [60, 45], [60, 180], [40, 180]]],
                        "lines": [f"日本語{index}"],
                    }
                ],
            }
        )
    if mokuro:
        (folder / f"{name}.mokuro").write_text(
            json.dumps({"title": name, "pages": pages}, ensure_ascii=False), encoding="utf-8"
        )
    return folder


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous_data = os.environ.get("BMO_DATA_DIR")
        self.previous_catalog_download = os.environ.get("BMO_DISABLE_CATALOG_DOWNLOAD")
        os.environ["BMO_DATA_DIR"] = str(self.root / "data")
        os.environ["BMO_DISABLE_CATALOG_DOWNLOAD"] = "1"
        server_module.initialize_data_dirs()
        server_module.DB = connect(server_module.data_dir() / "bilingual-manga.sqlite3")
        with server_module.BULK_LOCK:
            server_module.BULK_SCANS.clear()
        self.server = server_module.Server(("127.0.0.1", 0), server_module.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        server_module.DB.close()
        server_module.DB = None
        with server_module.BULK_LOCK:
            server_module.BULK_SCANS.clear()
        if self.previous_data is None:
            os.environ.pop("BMO_DATA_DIR", None)
        else:
            os.environ["BMO_DATA_DIR"] = self.previous_data
        if self.previous_catalog_download is None:
            os.environ.pop("BMO_DISABLE_CATALOG_DOWNLOAD", None)
        else:
            os.environ["BMO_DISABLE_CATALOG_DOWNLOAD"] = self.previous_catalog_download
        self.temporary.cleanup()

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None
        actual_headers = dict(headers or {})
        if body is not None and not isinstance(body, bytes):
            payload = json.dumps(body).encode()
            actual_headers["Content-Type"] = "application/json"
        else:
            payload = body
        connection.request(method, path, body=payload, headers=actual_headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        data = json.loads(raw) if "json" in content_type else raw
        connection.close()
        return response.status, data, dict(response.getheaders())

    def test_health_static_and_loopback_security_headers(self):
        status, data, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["loopback"])
        status, html, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Bilingual Manga", html)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, javascript, headers = self.request("GET", "/static/app.js?v=1.4.1")
        self.assertEqual(status, 200)
        self.assertIn(b"contentTypeName", javascript)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, _ = self.request("GET", "/api/library", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_reader_settings_survive_database_reopen(self):
        status, _, _ = self.request(
            "POST",
            "/api/settings",
            {
                "reader_fit": "width",
                "reader_zoom": 1.4,
                "reader_direction": "ltr",
                "reader_ocr_display": "visible",
                "reader_click_navigation": False,
            },
        )
        self.assertEqual(status, 200)
        status, settings, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(
            settings,
            {
                "reader_fit": "width",
                "reader_zoom": 1.4,
                "reader_direction": "ltr",
                "reader_ocr_display": "visible",
                "reader_click_navigation": False,
            },
        )
        with server_module.DB_LOCK:
            server_module.DB.close()
            server_module.DB = connect(server_module.data_dir() / "bilingual-manga.sqlite3")
        status, settings, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings["reader_fit"], "width")
        self.assertEqual(settings["reader_direction"], "ltr")

    def test_bulk_scan_import_isolated_failure_and_duplicate_detection(self):
        batch = self.root / "bulk"
        batch.mkdir()
        broken = make_edition(batch, "Broken Volume 01", True)
        good = make_edition(batch, "Good Volume 02", True)

        with mock.patch.object(server_module, "choose_bulk_folder", return_value=batch.resolve()):
            status, chosen, _ = self.request("POST", "/api/bulk/pick", {})
        self.assertEqual(status, 200)
        self.assertEqual(chosen["path"], str(batch.resolve()))

        status, started, _ = self.request(
            "POST", "/api/bulk/scan/start", {"path": str(batch)}
        )
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 5
        job = started
        while job["status"] == "scanning" and time.monotonic() < deadline:
            time.sleep(0.02)
            status, job, _ = self.request(
                "GET", f"/api/bulk/status?scan_id={started['id']}"
            )
            self.assertEqual(status, 200)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(len(job["items"]), 2)
        self.assertTrue(all(item["status"] == "ready" for item in job["items"]))

        broken_item = next(item for item in job["items"] if item["name"] == "Broken Volume 01.mokuro")
        good_item = next(item for item in job["items"] if item["name"] == "Good Volume 02.mokuro")
        for page in broken.glob("*.png"):
            page.unlink()
        status, failed, _ = self.request(
            "POST",
            "/api/bulk/import-one",
            {"scan_id": started["id"], "item_id": broken_item["id"], "metadata": {}},
        )
        self.assertEqual(status, 400)
        self.assertIn("images", failed["error"])

        status, imported, _ = self.request(
            "POST",
            "/api/bulk/import-one",
            {"scan_id": started["id"], "item_id": good_item["id"], "metadata": {}},
        )
        self.assertEqual(status, 201)
        self.assertEqual(imported["pages"], 3)
        status, duplicate, _ = self.request(
            "POST",
            "/api/bulk/import-one",
            {"scan_id": started["id"], "item_id": good_item["id"], "metadata": {}},
        )
        self.assertEqual(status, 400)
        self.assertIn("already", duplicate["error"])

        status, rescanned, _ = self.request(
            "POST", "/api/bulk/scan/start", {"path": str(batch)}
        )
        deadline = time.monotonic() + 5
        while rescanned["status"] == "scanning" and time.monotonic() < deadline:
            time.sleep(0.02)
            status, rescanned, _ = self.request(
                "GET", f"/api/bulk/status?scan_id={rescanned['id']}"
            )
        existing = next(item for item in rescanned["items"] if item["name"] == "Good Volume 02.mokuro")
        self.assertEqual(existing["status"], "existing")
        with server_module.DB_LOCK:
            row = server_module.DB.execute(
                "SELECT origin_path,import_signature FROM volumes WHERE id=?", (imported["id"],)
            ).fetchone()
        self.assertEqual(row["origin_path"], good_item["path"])
        self.assertEqual(row["import_signature"], good_item["signature"])

    def test_import_pair_toggle_navigation_progress_and_mokuro(self):
        jp = make_edition(self.root, "Demo Volume 1", True)
        en = make_edition(self.root, "Demo English", False)
        with server_module.DB_LOCK:
            jp_result = server_module.import_volume(jp, "jp", "Demo", "Demo JP", "Volume 1")
            en_result = server_module.import_volume(en, "en", "Demo", "Demo EN", "Volume 1")
        status, volume, _ = self.request("GET", f"/api/volume/{jp_result['id']}")
        self.assertEqual(status, 200)
        first_jp = volume["pages"][0]
        status, ocr, _ = self.request("GET", f"/api/mokuro/{first_jp['id']}")
        self.assertEqual(ocr["blocks"][0]["lines"], ["日本語0"])
        self.assertEqual(ocr["blocks"][0]["lines_coords"][0][0], [40, 45])
        status, match, _ = self.request("GET", f"/api/corresponding/{first_jp['id']}?side=jp")
        self.assertEqual(status, 200)
        self.assertEqual(match["volume_id"], en_result["id"])
        self.assertEqual(len(match["page_ids"]), 1)
        status, reverse, _ = self.request("GET", f"/api/corresponding/{match['page_ids'][0]}?side=en")
        self.assertEqual(reverse["page_ids"], [first_jp["id"]])
        status, progress, _ = self.request(
            "POST", "/api/progress", {"volume_id": jp_result["id"], "page_id": volume["pages"][2]["id"]}
        )
        self.assertEqual(status, 200)
        status, reopened, _ = self.request("GET", f"/api/volume/{jp_result['id']}")
        self.assertEqual(reopened["progress_page"], volume["pages"][2]["id"])

    def test_page_bookmark_is_returned_in_reader_and_library(self):
        source = make_edition(self.root, "Bookmarked", True)
        with server_module.DB_LOCK:
            volume_id = server_module.import_volume(source, "jp", "Bookmarked")["id"]
            page_id = server_module._page_rows(volume_id)[1]["id"]
        status, result, _ = self.request(
            "POST",
            "/api/bookmark",
            {"volume_id": volume_id, "page_id": page_id, "bookmarked": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["bookmark_count"], 1)
        status, volume, _ = self.request("GET", f"/api/volume/{volume_id}")
        self.assertEqual(volume["bookmark_page_ids"], [page_id])
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(library[0]["bookmark_count"], 1)
        status, result, _ = self.request(
            "POST",
            "/api/bookmark",
            {"volume_id": volume_id, "page_id": page_id, "bookmarked": False},
        )
        self.assertEqual(result["bookmark_count"], 0)

    def test_remove_edition_deletes_managed_copy_but_preserves_original_archive(self):
        page = self.root / "remove-page.png"
        make_page(page, 6)
        archive = self.root / "Remove Me Volume 01.cbz"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(page, "001.png")
        with server_module.DB_LOCK:
            result = server_module.import_volume(archive, "auto")
            volume = dict(
                server_module.DB.execute("SELECT * FROM volumes WHERE id=?", (result["id"],)).fetchone()
            )
        managed = Path(volume["source_path"])
        self.assertTrue(managed.is_dir())
        status, removed, _ = self.request(
            "POST", "/api/library/remove", {"volume_id": result["id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(removed["removed"], 1)
        self.assertTrue(removed["originals_preserved"])
        self.assertTrue(archive.is_file())
        self.assertFalse(managed.exists())
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(library, [])

    def test_remove_can_move_original_archive_to_trash(self):
        page = self.root / "trash-page.png"
        make_page(page, 8)
        archive = self.root / "Trash Me Volume 01.cbz"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(page, "001.png")
        with server_module.DB_LOCK:
            volume_id = server_module.import_volume(archive, "auto")["id"]
        trash = self.root / "trash"
        with mock.patch.dict(os.environ, {"BMO_TRASH_DIR": str(trash)}):
            status, removed, _ = self.request(
                "POST",
                "/api/library/remove",
                {"volume_id": volume_id, "delete_originals": True},
            )
        self.assertEqual(status, 200)
        self.assertEqual(removed["originals_trashed"], 1)
        self.assertFalse(archive.exists())
        self.assertTrue((trash / archive.name).is_file())

    def test_native_folder_import_can_make_friendly_copy_then_trash_original(self):
        source = make_edition(self.root, "Friendly Story Volume 04 English", False)
        trash = self.root / "trash"
        with mock.patch.dict(os.environ, {"BMO_TRASH_DIR": str(trash)}):
            status, result, _ = self.request(
                "POST",
                "/api/import",
                {
                    "path": str(source),
                    "language": "en",
                    "content_type": "raw",
                    "friendly_names": True,
                    "delete_original": True,
                },
            )
        self.assertEqual(status, 201)
        self.assertEqual(result["originals_trashed"], 1)
        self.assertFalse(source.exists())
        self.assertTrue((trash / source.name).is_dir())
        with server_module.DB_LOCK:
            volume = dict(server_module.DB.execute(
                "SELECT source_path,source_kind FROM volumes WHERE id=?", (result["id"],)
            ).fetchone())
        self.assertEqual(volume["source_kind"], "managed")
        managed_name = server_module._managed_root(volume["source_path"]).name
        self.assertIn("Friendly Story", managed_name)
        self.assertNotRegex(managed_name, r"^[0-9a-f-]{24,}$")

    def test_native_picker_endpoint_describes_selected_manga(self):
        source = make_edition(self.root, "Picker Manga", True)
        with mock.patch.object(server_module, "choose_manga_source", return_value=source):
            status, selected, _ = self.request("POST", "/api/source/pick", {})
        self.assertEqual(status, 200)
        self.assertEqual(selected["path"], str(source.resolve()))
        self.assertEqual(selected["kind"], "folder")
        self.assertTrue(selected["has_mokuro"])

    def test_ambiguous_opposite_language_match_is_confirmed_by_user(self):
        en_one = make_edition(self.root, "Pair Story East", False)
        en_two = make_edition(self.root, "Pair Story West", False)
        jp = make_edition(self.root, "Pair Story Japanese", True)
        with server_module.DB_LOCK:
            en_one_id = server_module.import_volume(
                en_one, "en", "Pair Story East", volume_label="Volume 1"
            )["id"]
            server_module.import_volume(
                en_two, "en", "Pair Story West", volume_label="Volume 1"
            )
            imported = server_module.import_volume(
                jp, "jp", "Pair Story", volume_label="Volume 1"
            )
        review = imported["pairing_review"]
        self.assertIsNotNone(review)
        self.assertGreaterEqual(len(review["candidates"]), 2)
        status, paired, _ = self.request(
            "POST",
            "/api/pairing/confirm",
            {"volume_id": imported["id"], "peer_id": en_one_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(paired["peer_id"], en_one_id)
        self.assertGreater(paired["pages_mapped"], 0)

    def test_remove_series_preserves_referenced_folders_and_cleans_mappings(self):
        jp = make_edition(self.root, "Remove Series JP", True)
        en = make_edition(self.root, "Remove Series EN", False)
        with server_module.DB_LOCK:
            server_module.import_volume(jp, "jp", "Remove Series", volume_label="Volume 1")
            server_module.import_volume(en, "en", "Remove Series", volume_label="Volume 1")
            self.assertGreater(server_module.DB.execute("SELECT COUNT(*) FROM mappings").fetchone()[0], 0)
        status, result, _ = self.request(
            "POST", "/api/library/remove", {"series": "Remove Series"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["removed"], 2)
        self.assertTrue(jp.is_dir())
        self.assertTrue(en.is_dir())
        with server_module.DB_LOCK:
            self.assertEqual(server_module.DB.execute("SELECT COUNT(*) FROM mappings").fetchone()[0], 0)

    def test_local_mokuro_conversion_updates_raw_volume_without_touching_source(self):
        source = make_edition(self.root, "Raw Volume 02", False)
        with server_module.DB_LOCK:
            volume_id = server_module.import_volume(source, "auto")["id"]
        job = server_module._new_conversion_job("Raw Volume 02", volume_id)

        def fake_run(_executable, pages, _root, _progress):
            output = pages.parent / "pages.mokuro"
            output.write_text(
                json.dumps(
                    {
                        "title": "Raw",
                        "pages": [
                            {"img_path": page.name, "img_width": 320, "img_height": 460, "blocks": []}
                            for page in sorted(pages.glob("*.png"))
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return output

        with (
            mock.patch.object(server_module, "ensure_engine", return_value=Path("/fake/python")),
            mock.patch.object(server_module, "run_mokuro", side_effect=fake_run),
        ):
            server_module._run_existing_volume_conversion(job["id"], volume_id)
        converted = server_module._conversion_snapshot(job["id"])
        self.assertEqual(converted["status"], "complete")
        with server_module.DB_LOCK:
            volume = dict(
                server_module.DB.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
            )
        self.assertEqual(volume["content_type"], "mokuro")
        self.assertTrue(Path(volume["mokuro_path"]).is_file())
        self.assertTrue(source.is_dir())

    def test_pdf_conversion_renders_ocr_and_imports_as_mokuro(self):
        managed = server_module.data_dir() / "library" / "pdf-import"
        managed.mkdir(parents=True)
        pdf = managed / "PDF Series Volume 03.pdf"
        pdf.write_bytes(b"pdf fixture")
        job = server_module._new_conversion_job(pdf.stem)

        def fake_render(_executable, _source, pages, _root, _progress):
            pages.mkdir()
            make_page(pages / "00001.png", 1)
            make_page(pages / "00002.png", 2)

        def fake_run(_executable, pages, _root, _progress):
            output = pages.parent / "pages.mokuro"
            output.write_text(
                json.dumps(
                    {
                        "title": "PDF Series",
                        "pages": [
                            {"img_path": page.name, "img_width": 320, "img_height": 460, "blocks": []}
                            for page in sorted(pages.glob("*.png"))
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return output

        with (
            mock.patch.object(server_module, "ensure_engine", return_value=Path("/fake/python")),
            mock.patch.object(server_module, "render_pdf", side_effect=fake_render),
            mock.patch.object(server_module, "run_mokuro", side_effect=fake_run),
        ):
            server_module._run_pdf_conversion(job["id"], pdf, {})
        converted = server_module._conversion_snapshot(job["id"])
        self.assertEqual(converted["status"], "complete")
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual((library[0]["content_type"], library[0]["page_count"]), ("mokuro", 2))
        self.assertEqual(library[0]["series"], "PDF Series")

    def test_different_volume_labels_are_not_accidentally_paired(self):
        jp = make_edition(self.root, "Different-JP", True)
        en = make_edition(self.root, "Different-EN", False)
        with server_module.DB_LOCK:
            jp_id = server_module.import_volume(jp, "jp", "Different", volume_label="Volume 1")["id"]
            server_module.import_volume(en, "en", "Different", volume_label="Volume 2")
            jp_page = server_module._page_rows(jp_id)[0]["id"]
        status, corresponding, _ = self.request(
            "GET", f"/api/corresponding/{jp_page}?side=jp"
        )
        self.assertEqual(status, 200)
        self.assertEqual(corresponding["page_ids"], [])

    def test_manual_correction_and_grouped_mapping_persist(self):
        jp = make_edition(self.root, "JP", True)
        en = make_edition(self.root, "EN", False)
        with server_module.DB_LOCK:
            jp_id = server_module.import_volume(jp, "jp", "Grouped")["id"]
            en_id = server_module.import_volume(en, "en", "Grouped")["id"]
            jp_pages = server_module._page_rows(jp_id)
            en_pages = server_module._page_rows(en_id)
        status, result, _ = self.request(
            "POST",
            "/api/map",
            {"series": "Grouped", "jp_pages": [jp_pages[0]["id"]], "en_pages": [en_pages[0]["id"], en_pages[1]["id"]]},
        )
        self.assertEqual(status, 200)
        status, match, _ = self.request("GET", f"/api/corresponding/{jp_pages[0]['id']}?side=jp")
        self.assertEqual(match["page_ids"], [en_pages[0]["id"], en_pages[1]["id"]])
        with server_module.DB_LOCK:
            row = server_module.DB.execute("SELECT manual FROM mappings WHERE jp_pages LIKE ?", (f'%{jp_pages[0]["id"]}%',)).fetchone()
        self.assertEqual(row["manual"], 1)

    def test_missing_source_and_relink_keep_page_identity(self):
        source = make_edition(self.root, "Moved", True)
        with server_module.DB_LOCK:
            volume_id = server_module.import_volume(source, "jp", "Moved")["id"]
            before = [page["id"] for page in server_module._page_rows(volume_id)]
        moved = self.root / "移動後"
        source.rename(moved)
        status, library, _ = self.request("GET", "/api/library")
        self.assertFalse(next(item for item in library if item["id"] == volume_id)["available"])
        with server_module.DB_LOCK:
            server_module.relink_volume(volume_id, moved)
            after = [page["id"] for page in server_module._page_rows(volume_id)]
        self.assertEqual(before, after)
        status, library, _ = self.request("GET", "/api/library")
        self.assertTrue(next(item for item in library if item["id"] == volume_id)["available"])

    def test_rebranded_data_folder_rewrites_managed_absolute_paths(self):
        legacy = self.root / "Bilingual Manga Offline"
        (legacy / "library").mkdir(parents=True)
        source = make_edition(legacy / "library", "Migrated", True)
        with server_module.DB_LOCK:
            volume_id = server_module.import_volume(source, "jp", "Migrated")["id"]
        renamed = self.root / "Mokuro & Bilingual Manga Reader"
        legacy.rename(renamed)
        with server_module.DB_LOCK:
            changed = server_module._migrate_legacy_paths(renamed, legacy)
            volume = dict(
                server_module.DB.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
            )
        self.assertGreaterEqual(changed, 1)
        self.assertTrue(volume["source_path"].startswith(str(renamed.resolve())))
        self.assertTrue(Path(volume["cover_path"]).is_file())
        self.assertTrue(server_module.library_rows()[0]["available"])

    def test_streaming_upload_unicode_and_database_backup(self):
        status, started, _ = self.request(
            "POST", "/api/upload/start", {"language": "jp", "series": "日本語", "title": "第一巻"}
        )
        upload_id = started["upload_id"]
        fixture = self.root / "upload.png"
        make_page(fixture, 2)
        status, _, _ = self.request(
            "POST",
            f"/api/upload/file?upload_id={upload_id}",
            fixture.read_bytes(),
            {"X-Relative-Path": "%E6%97%A5%E6%9C%AC%E8%AA%9E%2F001.png"},
        )
        self.assertEqual(status, 201)
        mokuro = json.dumps({"title": "第一巻", "pages": []}, ensure_ascii=False).encode()
        status, _, _ = self.request(
            "POST",
            f"/api/upload/file?upload_id={upload_id}",
            mokuro,
            {"X-Relative-Path": "%E6%97%A5%E6%9C%AC%E8%AA%9E%2Fbook.mokuro"},
        )
        self.assertEqual(status, 201)
        status, result, _ = self.request("POST", "/api/upload/finish", {"upload_id": upload_id})
        self.assertEqual(status, 201)
        self.assertEqual(result["pages"], 1)
        status, backup, headers = self.request("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertTrue(backup.startswith(b"SQLite format 3"))
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_archive_upload_uses_mokuro_title_for_series_when_blank(self):
        page = self.root / "001.png"
        make_page(page, 3)
        archive = self.root / "mokuro-volume.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(page, "source-folder/001.png")
            bundle.writestr(
                "source-folder/book.mokuro",
                json.dumps({"title": "Archive Mokuro Title", "pages": []}, ensure_ascii=False),
            )
        generated_ids = [
            server_module.uuid.UUID("11111111-1111-4111-8111-111111111111"),
            # This is a valid UUID, but infer_metadata would parse its final
            # "-c55" as a chapter marker if the inferred title were tested.
            server_module.uuid.UUID("22222222-2222-4222-8222-222222222c55"),
            server_module.uuid.UUID("33333333-3333-4333-8333-333333333333"),
        ]
        with mock.patch.object(server_module.uuid, "uuid4", side_effect=generated_ids):
            status, started, _ = self.request("POST", "/api/upload/start", {"language": "jp"})
            self.assertEqual(status, 201)
            status, _, _ = self.request(
                "POST",
                f"/api/upload/file?upload_id={started['upload_id']}",
                archive.read_bytes(),
                {"X-Relative-Path": "mokuro-volume.zip"},
            )
            self.assertEqual(status, 201)
            status, result, _ = self.request(
                "POST", "/api/upload/finish", {"upload_id": started["upload_id"]}
            )
        self.assertEqual(status, 201)
        self.assertEqual(result["pages"], 1)
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(library[0]["series"], "Archive Mokuro Title")

    def test_raw_archive_type_language_and_metadata_are_inferred(self):
        page = self.root / "auto-page.png"
        make_page(page, 9)
        archive = self.root / "Friendly Series Volume 07 [English].cbz"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(page, "001.png")
        status, started, _ = self.request("POST", "/api/upload/start", {"language": "auto"})
        status, _, _ = self.request(
            "POST",
            f"/api/upload/file?upload_id={started['upload_id']}",
            archive.read_bytes(),
            {"X-Relative-Path": "Friendly%20Series%20Volume%2007%20%5BEnglish%5D.cbz"},
        )
        status, result, _ = self.request(
            "POST", "/api/upload/finish", {"upload_id": started["upload_id"]}
        )
        self.assertEqual(status, 201)
        self.assertEqual((result["content_type"], result["language"]), ("raw", "en"))
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(library[0]["series"], "Friendly Series")
        self.assertEqual(library[0]["volume_label"], "Volume 07")

    def test_tar_upload_imports_as_japanese_only_mokuro(self):
        page = self.root / "001.png"
        make_page(page, 4)
        archive = self.root / "bafybeidh6ejstwf2fku7z63szbj57tpsi7tvd6kpea5sas6e7c2rlflcvy.tar"
        mokuro = json.dumps(
            {
                "title": "TAR Mokuro Title",
                "pages": [
                    {
                        "img_path": "001.png",
                        "img_width": 320,
                        "img_height": 460,
                        "blocks": [],
                    }
                ],
            },
            ensure_ascii=False,
        ).encode()
        with tarfile.open(archive, "w") as bundle:
            bundle.add(page, arcname="source-folder/001.png")
            info = tarfile.TarInfo("source-folder/book.mokuro")
            info.size = len(mokuro)
            bundle.addfile(info, io.BytesIO(mokuro))

        status, started, _ = self.request("POST", "/api/upload/start", {"language": "jp"})
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "POST",
            f"/api/upload/file?upload_id={started['upload_id']}",
            archive.read_bytes(),
            {"X-Relative-Path": archive.name},
        )
        self.assertEqual(status, 201)
        status, result, _ = self.request(
            "POST", "/api/upload/finish", {"upload_id": started["upload_id"]}
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["mokuro"])

        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(len(library), 1)
        self.assertEqual(library[0]["series"], "TAR Mokuro Title")
        self.assertEqual(library[0]["language"], "jp")
        status, volume, _ = self.request("GET", f"/api/volume/{result['id']}")
        self.assertEqual(status, 200)
        status, corresponding, _ = self.request(
            "GET", f"/api/corresponding/{volume['pages'][0]['id']}?side=jp"
        )
        self.assertEqual(status, 200)
        self.assertEqual(corresponding["page_ids"], [])

    def test_archive_collection_folder_gets_bulk_scan_guidance(self):
        collection = self.root / "tar_ipfs"
        collection.mkdir()
        for index in range(2):
            archive = collection / f"bafy-collection-{index}.tar"
            page = self.root / f"collection-{index}.png"
            make_page(page, index + 20)
            with tarfile.open(archive, "w") as bundle:
                bundle.add(page, arcname="001.png")

        with self.assertRaisesRegex(ValueError, r"2 manga archives.*Use Scan folder"):
            server_module.import_volume(collection, "jp")

    def test_legacy_library_scan_does_not_duplicate_managed_archive_imports(self):
        page = self.root / "scan-page.png"
        make_page(page, 17)
        archive = self.root / "Scan Safety Volume 01.tar"
        with tarfile.open(archive, "w") as bundle:
            bundle.add(page, arcname="001.png")

        status, imported, _ = self.request(
            "POST",
            "/api/import",
            {"path": str(archive), "language": "jp"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(imported["pages"], 1)

        status, scanned, _ = self.request("POST", "/api/scan", {})
        self.assertEqual(status, 200)
        self.assertEqual(scanned, {"imported": [], "errors": []})
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(len(library), 1)

    def test_user_catalog_recognizes_cid_tar_and_overrides_language_metadata(self):
        cid = "bafybeidh6ejstwf2fku7z63szbj57tpsi7tvd6kpea5sas6e7c2rlflcvy"
        catalog_zip = self.root / "json.zip"
        manga_data = [
            {
                "_id": {"$oid": "record-1"},
                "jp_data": {"ch_jph": [], "ch_najp": []},
                "en_data": {
                    "ch_enh": [f"{cid}/hash/%@rep@%"],
                    "ch_naen": ["Volume 03"],
                },
            }
        ]
        metadata = [{"manga_titles": [{"enid": "record-1", "entit": "Recognized Series"}]}]
        with zipfile.ZipFile(catalog_zip, "w") as bundle:
            bundle.writestr("json/BM_data.manga_data.json", json.dumps(manga_data))
            bundle.writestr("json/BM_data.manga_metadata.json", json.dumps(metadata))
        status, installed, _ = self.request(
            "POST",
            "/api/catalog/import",
            catalog_zip.read_bytes(),
            {"Content-Type": "application/zip"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(installed["archives"], 1)

        page = self.root / "catalog-page.png"
        make_page(page, 5)
        archive = self.root / f"{cid}.tar"
        with tarfile.open(archive, "w") as bundle:
            bundle.add(page, arcname="hash/001.png")
        status, started, _ = self.request("POST", "/api/upload/start", {"language": "jp"})
        status, _, _ = self.request(
            "POST",
            f"/api/upload/file?upload_id={started['upload_id']}",
            archive.read_bytes(),
            {"X-Relative-Path": archive.name},
        )
        status, result, _ = self.request(
            "POST", "/api/upload/finish", {"upload_id": started["upload_id"]}
        )
        self.assertEqual(status, 201)
        self.assertTrue(result["bilingual_archive"])
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(library[0]["language"], "en")
        self.assertEqual(library[0]["content_type"], "bilingual")
        self.assertEqual(library[0]["series"], "Recognized Series")
        self.assertEqual(library[0]["volume_label"], "Volume 03")

    def test_archived_mapping_import_validates_local_pages(self):
        jp = make_edition(self.root, "JP-map", True)
        en = make_edition(self.root, "EN-map", False)
        with server_module.DB_LOCK:
            server_module.import_volume(jp, "jp", "Archive")
            server_module.import_volume(en, "en", "Archive")
        payload = {"title": "Archive", "img_data": {"jp": {"0_0": "0_0.jpg"}, "en": {}}}
        status, result, _ = self.request(
            "POST", "/api/mappings/import", {"series": "Archive", "data": payload}
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["unresolved"], 0)

    def test_large_library_query_is_bounded(self):
        cover = self.root / "cover.png"
        make_page(cover, 0)
        with server_module.DB_LOCK:
            for index in range(300):
                volume = f"volume-{index}"
                server_module.DB.execute(
                    "INSERT INTO volumes(id,title,series,language,source_path,page_count,cover_path) VALUES(?,?,?,?,?,?,?)",
                    (volume, f"Title {index}", f"Series {index}", "jp", str(self.root), 1, str(cover)),
                )
                server_module.DB.execute(
                    "INSERT INTO pages(id,volume_id,ordinal,path) VALUES(?,?,0,?)",
                    (f"page-{index}", volume, str(cover)),
                )
            server_module.DB.commit()
        started = time.monotonic()
        status, library, _ = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(len(library), 300)
        self.assertLess(time.monotonic() - started, 2.0)


class OfflineSourceTests(unittest.TestCase):
    def test_frontend_has_no_remote_runtime_dependency(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "static" / "app.js").read_text()
        combined = (root / "static" / "index.html").read_text() + app_source
        archive_url = "https://github.com/B-M-dev/Bilingual-Manga-archive"
        self.assertGreaterEqual(combined.count(archive_url), 4)
        runtime_source = combined.replace(archive_url, "")
        self.assertNotIn("https://", runtime_source)
        self.assertNotIn('src="http', runtime_source)
        self.assertNotIn("cdn", combined.lower())
        self.assertIn("block.lines_coords", app_source)
        self.assertIn('text.className = "ocr-line"', app_source)
        self.assertIn("Bilingual Manga Reader and Mokuro Converter", combined)
        self.assertIn('class="bilingual-mark"', app_source)
        self.assertIn('data-action="bookmark"', app_source)
        self.assertIn('data-action="ocr-display"', app_source)
        self.assertIn('data-action="click-navigation"', app_source)
        self.assertIn('data-action="library-filter"', app_source)
        self.assertIn("Scan for manga", combined)
        self.assertNotIn('id="choose-folder"', combined)
        self.assertNotIn("<label>Language", combined)
        self.assertIn("Edition type", combined)
        self.assertIn("Stored only on this computer", combined)
        self.assertIn('data-action="remove-volume"', app_source)
        self.assertIn('data-action="remove-series"', app_source)
        self.assertIn('data-action="convert"', app_source)
        self.assertIn('id="friendly-names"', combined)
        self.assertIn('id="delete-original-after-import"', combined)
        self.assertIn('id="remove-originals"', combined)
        self.assertIn('class="reader-nav"', app_source)
        self.assertIn('discoveryButton("mokuro-ready", "Mokuro-Ready")', app_source)
        self.assertNotIn("Most bookmarked", combined)
        self.assertNotIn("/api/catalog/import", app_source)
        self.assertIn("STABLE_PORT = 8765", app_source)
        self.assertIn("/api/source/pick", app_source)
        self.assertIn("/api/bulk/scan/start", app_source)
        self.assertIn("/api/bulk/import-one", app_source)
        self.assertIn("state.bulk.importing = false;\n  await showLibrary();", app_source)

    def test_ubunchu_sample_bubble_uses_pixel_calibrated_line_boxes(self):
        root = Path(__file__).resolve().parents[1]
        fixture = json.loads(
            (
                root
                / "outputs"
                / "mokuro-test-fixtures"
                / "Ubunchu-Episode-01-JP"
                / "Ubunchu-Episode-1.mokuro"
            ).read_text(encoding="utf-8")
        )
        bubble = next(
            block
            for block in fixture["pages"][2]["blocks"]
            if block.get("lines") == ["一瞬くらい", "検討して", "くださいよー！"]
        )
        self.assertEqual(bubble["font_size"], 30)
        self.assertEqual(
            bubble["lines_coords"],
            [
                [[176, 1315], [210, 1315], [210, 1465], [176, 1465]],
                [[130, 1305], [164, 1305], [164, 1435], [130, 1435]],
                [[82, 1305], [116, 1305], [116, 1525], [82, 1525]],
            ],
        )


if __name__ == "__main__":
    unittest.main()
