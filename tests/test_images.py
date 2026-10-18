"""Image cards: what show() takes as a picture, the files a card keeps, and how
they reach the board and the exports."""

import io
import json
import zipfile

import numpy as np
import pytest
from PIL import Image

from vitrine._types import CardType, Picture
from vitrine.artifacts import ArtifactStore
from vitrine.export import export_html_string, export_json
from vitrine.renderer import render
from vitrine.server import DisplayServer
from vitrine.study_manager import StudyManager


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(session_dir=tmp_path / "images_session", session_id="images")


def _files(store, card):
    """The pictures' files, opened, by preview key, for each picture."""
    return [
        {
            key: store.card_file_path(card.card_id, entry[key])
            for key in ("file", "thumb", "overlay", "overlay_thumb")
            if key in entry
        }
        for entry in card.preview["images"]
    ]


def _xray(height=600, width=500):
    """A grayscale picture with a gradient, so scaling is visible."""
    return np.tile(np.linspace(0, 255, width, dtype=np.uint8), (height, 1))


class TestWhatIsAPicture:
    def test_pil_image_makes_an_image_card(self, store):
        card = render(Image.fromarray(_xray()), store=store)
        assert card.card_type == CardType.IMAGE
        assert card.preview["layout"] == "single"
        [entry] = card.preview["images"]
        assert (entry["width"], entry["height"]) == (500, 600)

    def test_array_makes_an_image_card(self, store):
        card = render(_xray(), store=store)
        assert card.card_type == CardType.IMAGE

    def test_small_array_stays_data(self, store):
        card = render(np.arange(15).reshape(5, 3), store=store)
        assert card.card_type == CardType.MARKDOWN

    def test_image_file_path_makes_an_image_card(self, store, tmp_path):
        path = tmp_path / "film.png"
        Image.fromarray(_xray()).save(path)
        assert render(path, store=store).card_type == CardType.IMAGE

    def test_other_path_stays_repr(self, store, tmp_path):
        card = render(tmp_path / "notes.txt", store=store)
        assert card.card_type == CardType.MARKDOWN

    def test_list_of_pictures_makes_a_gallery(self, store):
        card = render([_xray(), Image.fromarray(_xray())], store=store)
        assert card.preview["layout"] == "gallery"
        assert len(card.preview["images"]) == 2
        assert card.title == "2 images"

    def test_mixed_list_stays_repr(self, store):
        card = render([_xray(), "not a picture"], store=store)
        assert card.card_type == CardType.MARKDOWN

    def test_dicom_is_refused_with_a_way_out(self, store, tmp_path):
        with pytest.raises(ValueError, match="DICOM"):
            render(Picture(tmp_path / "film.dcm"), store=store)


class TestStoredFiles:
    def test_index_holds_names_not_pixels(self, store):
        card = render(Image.fromarray(_xray(3000, 2500)), store=store)
        assert len(json.dumps(card.preview)) < 500
        assert "data" not in card.preview

    def test_file_keeps_its_own_bytes(self, store, tmp_path):
        path = tmp_path / "film.jpg"
        Image.fromarray(_xray()).save(path, quality=90)
        card = render(path, store=store)
        [files] = _files(store, card)
        assert files["file"].read_bytes() == path.read_bytes()
        assert files["file"].suffix == ".jpg"

    def test_thumbnail_is_small(self, store):
        card = render(_xray(3000, 2500), store=store)
        [files] = _files(store, card)
        with Image.open(files["thumb"]) as thumb:
            assert max(thumb.size) == 512

    def test_16_bit_array_is_scaled_to_8_bits(self, store):
        raw = np.tile(np.linspace(1000, 4000, 200), (100, 1)).astype(np.uint16)
        card = render(raw, store=store)
        [files] = _files(store, card)
        with Image.open(files["file"]) as img:
            pixels = np.asarray(img)
        assert img.mode == "L"
        assert pixels.min() == 0 and pixels.max() == 255

    def test_channels_first_array_is_read_as_rgb(self, store):
        card = render(Picture(np.zeros((3, 40, 30), dtype=np.uint8)), store=store)
        [entry] = card.preview["images"]
        assert (entry["width"], entry["height"]) == (30, 40)

    def test_caption_and_labels(self, store):
        card = render(
            [
                Picture(
                    _xray(),
                    caption="p10 s5",
                    labels={"Effusion": 0.81234, "Truth": "positive"},
                )
            ],
            store=store,
        )
        [entry] = card.preview["images"]
        assert entry["caption"] == "p10 s5"
        assert entry["labels"] == {"Effusion": "0.812", "Truth": "positive"}


class TestOverlays:
    def test_named_masks_get_colours_and_a_legend(self, store):
        lungs = np.zeros((60, 50), dtype=bool)
        lungs[10:50, 5:20] = True
        heart = np.zeros((60, 50), dtype=bool)
        heart[30:50, 20:35] = True
        card = render(
            Picture(_xray(60, 50), overlay={"Lungs": lungs, "Heart": heart}),
            store=store,
        )
        [entry] = card.preview["images"]
        assert [name for name, _ in entry["legend"]] == ["Lungs", "Heart"]
        [files] = _files(store, card)
        with Image.open(files["overlay"]) as overlay:
            rgba = np.asarray(overlay)
        assert overlay.mode == "RGBA"
        assert rgba[20, 10, 3] > 0  # inside the lungs
        assert rgba[5, 45, 3] == 0  # outside both masks

    def test_mask_is_resized_to_the_image(self, store):
        mask = np.ones((10, 10), dtype=bool)
        card = render(Picture(_xray(200, 100), overlay=mask), store=store)
        [files] = _files(store, card)
        with Image.open(files["overlay"]) as overlay:
            assert overlay.size == (100, 200)

    def test_heatmap_alpha_follows_the_values(self, store):
        heat = np.tile(np.linspace(0, 1, 50), (60, 1))
        card = render(Picture(_xray(60, 50), overlay=heat), store=store)
        [files] = _files(store, card)
        with Image.open(files["overlay"]) as overlay:
            alpha = np.asarray(overlay)[..., 3]
        assert alpha[0, 0] == 0
        assert alpha[0, -1] > alpha[0, 25] > 0


class TestReplace:
    def test_replacing_keeps_the_card_id_on_every_file(self, store):
        first = render(_xray(), store=store)
        second = render([_xray(), _xray()], store=store)
        store.update_card(first.card_id, preview=second.preview)
        store.adopt_artifacts(second.card_id, first.card_id)
        names = sorted(p.name for p in store._artifacts_dir.iterdir())
        assert names and all(n.startswith(f"{first.card_id}.") for n in names)
        for entry in second.preview["images"]:
            assert store.card_file_path(first.card_id, entry["file"]).is_file()


class TestRevisions:
    def test_a_replaced_gallery_keeps_every_file(self, store):
        card = render([_xray(), _xray(60, 50)], store=store)
        store.store_card(card)
        owned = sorted(p.name for p in store._artifacts_dir.glob(f"{card.card_id}.*"))
        assert store.keep_revision(card.card_id) == 1
        [revision] = store.list_revisions(card.card_id)
        assert len(revision["artifacts"]) == len(owned) == 4

    def test_the_same_pictures_count_as_the_same_content(self, store):
        first = render([_xray()], store=store)
        second = render([_xray()], store=store)
        third = render([_xray(60, 50)], store=store)
        for card in (first, second, third):
            store.store_card(card)
        assert store.same_content(first.card_id, second.card_id)
        assert not store.same_content(first.card_id, third.card_id)


class TestImageRoute:
    @pytest.fixture
    def client(self, store):
        from starlette.testclient import TestClient

        server = DisplayServer(
            store=store, port=7797, host="127.0.0.1", session_id="images"
        )
        return TestClient(server._app)

    def test_serves_a_thumbnail_for_good(self, client, store):
        card = render(_xray(), store=store)
        name = card.preview["images"][0]["thumb"]
        resp = client.get(f"/api/image/{card.card_id}/{name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert "immutable" in resp.headers["cache-control"]
        assert Image.open(io.BytesIO(resp.content)).size[0] <= 512

    @pytest.mark.parametrize(
        "name",
        ["../index.json", "image-0-zzzzzzzz.png", "notes.txt", "image-0-0123abcd.png"],
    )
    def test_refuses_names_it_never_wrote(self, client, store, name):
        card = render(_xray(), store=store)
        assert client.get(f"/api/image/{card.card_id}/{name}").status_code == 404


class TestExports:
    @pytest.fixture
    def manager(self, tmp_path):
        manager = StudyManager(tmp_path / "display")
        _, store = manager.get_or_create_study("films")
        card = render(
            [
                Picture(
                    _xray(), caption="first", overlay=np.ones((10, 10), dtype=bool)
                ),
                _xray(),
            ],
            store=store,
            study="films",
        )
        manager.register_card(card.card_id, manager._label_to_dir["films"])
        manager.card = card
        return manager

    def test_json_export_carries_every_file(self, manager, tmp_path):
        out = export_json(manager, tmp_path / "films.zip", study="films")
        with zipfile.ZipFile(out) as zf:
            names = {
                n.removeprefix("artifacts/")
                for n in zf.namelist()
                if n.startswith("artifacts/")
            }
        expected = {
            f"{manager.card.card_id}.{entry[key]}"
            for entry in manager.card.preview["images"]
            for key in ("file", "thumb", "overlay", "overlay_thumb")
            if key in entry
        }
        assert names == expected

    def test_html_export_inlines_thumbnails(self, manager):
        html = export_html_string(manager, study="films")
        assert html.count('class="picture"') == 2
        assert html.count("data:image/jpeg;base64,") == 2
        assert 'class="picture-overlay"' in html
        assert "<figcaption>first</figcaption>" in html


SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">'
    '<script>alert(1)</script><rect width="320" height="200" fill="teal"/></svg>'
)


class TestSvgPictures:
    def test_svg_file_is_its_own_thumbnail(self, store, tmp_path):
        path = tmp_path / "flow.svg"
        path.write_text(SVG)
        card = render(path, store=store)
        [entry] = card.preview["images"]
        assert entry["file"].endswith(".svg")
        assert entry["thumb"] == entry["file"]
        assert (entry["width"], entry["height"]) == (320, 200)

    def test_svg_is_sanitized(self, store, tmp_path):
        path = tmp_path / "flow.svg"
        path.write_text(SVG)
        card = render(path, store=store)
        [files] = _files(store, card)
        assert b"<script" not in files["file"].read_bytes()

    def test_svg_size_falls_back_to_the_viewbox(self, store, tmp_path):
        path = tmp_path / "flow.svg"
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480"></svg>'
        )
        [entry] = render(path, store=store).preview["images"]
        assert (entry["width"], entry["height"]) == (640, 480)

    def test_svg_is_served_without_script(self, store, tmp_path):
        from starlette.testclient import TestClient

        path = tmp_path / "flow.svg"
        path.write_text(SVG)
        card = render(path, store=store)
        server = DisplayServer(
            store=store, port=7796, host="127.0.0.1", session_id="images"
        )
        resp = TestClient(server._app).get(
            f"/api/image/{card.card_id}/{card.preview['images'][0]['file']}"
        )
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "default-src 'none'" in resp.headers["content-security-policy"]


class TestMarkdownImages:
    def test_local_image_is_copied_into_the_card(self, store, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Image.fromarray(_xray()).save(tmp_path / "plot.png")
        card = render("Result:\n\n![ROC](plot.png)", store=store)
        assert card.card_type == CardType.MARKDOWN
        [name] = [
            n.removeprefix("vitrine-image:")
            for n in card.preview["text"].split("(")[1:]
            if n.startswith("vitrine-image:")
        ]
        name = name.rstrip(")")
        path = store.card_file_path(card.card_id, name)
        assert path.read_bytes() == (tmp_path / "plot.png").read_bytes()
        assert card.artifact_id == card.card_id

    def test_html_img_and_repeated_images(self, store, tmp_path):
        png = tmp_path / "a.png"
        Image.fromarray(_xray()).save(png)
        card = render(f'![a]({png}) <img src="{png}" width="200">', store=store)
        assert card.preview["text"].count("vitrine-image:image-0-") == 2
        assert len(list(store._artifacts_dir.glob(f"{card.card_id}.*"))) == 1

    def test_web_addresses_and_missing_files_stay(self, store):
        text = "![a](https://example.org/a.png) ![b](data:image/png;base64,AA==) ![c](gone.png)"
        card = render(text, store=store)
        assert card.preview["text"] == text
        assert card.artifact_id is None

    def test_repr_cards_are_not_mined_for_images(self, store, tmp_path):
        png = tmp_path / "a.png"
        Image.fromarray(_xray()).save(png)
        card = render([f"![a]({png})", 1], store=store)
        assert "vitrine-image:" not in card.preview["text"]

    def test_exports_carry_markdown_images(self, tmp_path):
        manager = StudyManager(tmp_path / "display")
        _, store = manager.get_or_create_study("notes")
        png = tmp_path / "plot.png"
        Image.fromarray(_xray()).save(png)
        card = render(f"![plot]({png})", store=store, study="notes")
        manager.register_card(card.card_id, manager._label_to_dir["notes"])
        html = export_html_string(manager, study="notes")
        assert "vitrine-image:" not in html
        assert "data:image/png;base64," in html
        out = export_json(manager, tmp_path / "notes.zip", study="notes")
        with zipfile.ZipFile(out) as zf:
            assert any(
                n.startswith(f"artifacts/{card.card_id}.image-0-")
                for n in zf.namelist()
            )
