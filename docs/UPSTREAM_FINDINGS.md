# Upstream findings

Investigated 2026-08-28: `B-M-dev/Bilingual-Manga-archive` and `B-M-dev/Bilingual_Manga-home-`.

The archive is a Django port of the old SvelteKit/Express site. Its JSON metadata stores title records and per-language `img_data` page structures. The prior reader synchronized active/inactive language pages, supported `L`, 2-page spreads, sliders, and a similarity-based mode. It relied on content torrents/IPFS-style image paths and is therefore not reused as application code or bundled data. The current product imports only user-supplied content and keeps a normalized mapping importer boundary.

Mokuro compatibility is implemented as an integration target, not an OCR replacement: a JP import records the `.mokuro` file and its image directory. Current Mokuro `.mokuro` files contain a `pages` array keyed by `img_path`; each page carries `img_width`, `img_height`, and OCR `blocks` with boxes, lines, font size, and vertical orientation. The reader renders those as selectable positioned DOM spans; raw OCR is never flattened.

No license file was present at the inspected Bilingual Manga archive root. No archive data or code is redistributed; therefore no upstream content license is asserted. Upstream mapping import is intentionally opt-in and must validate its licensing before any dataset is shipped.
