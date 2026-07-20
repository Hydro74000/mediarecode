from pathlib import Path

from core.workflows.remux_backend import native_preparation_commands, select_mux_backend
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.matroska.ebml import ascii_element, element, uint_element
from core.matroska.ids import (
    EBML_HEADER_ID, SEGMENT_ID, TRACKS_ID, TRACK_ENTRY_ID, TRACK_NUMBER_ID,
    TRACK_UID_ID, TRACK_TYPE_ID, CODEC_ID_ID,
)


def track(index: int, kind: str = "video") -> TrackEntry:
    return TrackEntry(index, kind, "COPY", "", "und", "", file_id="src0")


def config(tmp_path: Path, *, backend: str = "auto", suffix: str = ".mkv") -> RemuxConfig:
    source = tmp_path / f"source{suffix}"
    if suffix == ".mkv":
        entry = element(TRACK_ENTRY_ID, b"".join((
            uint_element(TRACK_NUMBER_ID, 1), uint_element(TRACK_UID_ID, 1),
            uint_element(TRACK_TYPE_ID, 1), ascii_element(CODEC_ID_ID, "V_MPEG4/ISO/AVC"),
        )))
        source.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(TRACKS_ID, entry))
    else:
        source.write_bytes(b"fixture")
    return RemuxConfig(
        sources=[SourceInput(source, 0, [track(0)])], output=tmp_path / "out.mkv",
        track_order=[(0, 0)], keep_chapters=False, mux_backend=backend,
    )


def test_auto_selects_native_for_mkv_and_non_mkv_inputs(tmp_path: Path) -> None:
    assert select_mux_backend(config(tmp_path)).selected == "native"
    assert select_mux_backend(config(tmp_path, suffix=".mp4")).selected == "native"


def test_ffmpeg_override_remains_strictly_backward_compatible(tmp_path: Path) -> None:
    decision = select_mux_backend(config(tmp_path, backend="ffmpeg"))
    assert decision.requested == decision.selected == "ffmpeg"


def test_native_rejects_non_mkv_output(tmp_path: Path) -> None:
    cfg = config(tmp_path, backend="native")
    cfg.output = tmp_path / "out.mp4"
    decision = select_mux_backend(cfg)
    assert decision.native_reasons


def test_auto_falls_back_for_unreadable_matroska(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.sources[0].path.write_bytes(b"not-ebml")
    decision = select_mux_backend(cfg)
    assert decision.selected == "ffmpeg"
    assert "illisible" in decision.native_reasons[0]


def test_all_matroska_family_extensions_are_consumed_directly(tmp_path: Path) -> None:
    cfg = config(tmp_path, suffix=".mp4")
    for suffix in (".mkv", ".webm", ".mka", ".mks", ".mk3d"):
        cfg.sources[0].path = tmp_path / f"source{suffix}"
        assert native_preparation_commands(cfg, "ffmpeg") == []


class TestOutputContractAttachmentNaming:
    """P2 audit externe : le contrat attend exactement les noms produits.

    - backend FFmpeg : ``filename=`` complété par l'extension MIME quand
      absente ; attached-pictures extraites (réencodées) → nom répliqué,
      taille et MIME de la source non exigés ;
    - backend natif : copie brute, noms de la source inchangés.
    """

    def _config_with_attachment(self, tmp_path: Path, *, backend: str, **attachment_kw) -> RemuxConfig:
        from core.inspector import AttachmentInfo

        base = config(tmp_path, backend=backend)
        attachment = AttachmentInfo(**{
            "index": 5, "local_index": 0, "filename": "cover",
            "mimetype": "image/jpeg", "size_bytes": 4321,
            "is_attached_pic": False,
            **attachment_kw,
        })
        base.sources[0].selected_attachments = [attachment]
        return base

    @staticmethod
    def _contract(cfg: RemuxConfig):
        from core.workflows.remux_plan import plan_remux

        return plan_remux(cfg).output_contract

    def test_ffmpeg_contract_completes_extension_from_mime(self, tmp_path: Path) -> None:
        cfg = self._config_with_attachment(tmp_path, backend="ffmpeg")
        contract = self._contract(cfg)
        assert contract.attachment_names == ("cover.jpg",)
        assert contract.expected_attachments[0].size == 4321

    def test_native_contract_keeps_source_name(self, tmp_path: Path) -> None:
        cfg = self._config_with_attachment(tmp_path, backend="native")
        contract = self._contract(cfg)
        assert contract.attachment_names == ("cover",)

    def test_ffmpeg_attached_pic_expectation_is_size_agnostic(self, tmp_path: Path) -> None:
        """Image extraite puis réencodée : nom répliqué, taille/MIME non exigés."""
        cfg = self._config_with_attachment(
            tmp_path, backend="ffmpeg", is_attached_pic=True,
        )
        contract = self._contract(cfg)
        assert contract.attachment_names == ("cover.jpg",)
        expectation = contract.expected_attachments[0]
        assert expectation.size is None
        assert expectation.media_type is None

    def test_ffmpeg_pic_collisions_are_case_insensitive(self, tmp_path: Path) -> None:
        """cover.jpg puis COVER.JPG → COVER_1.jpg, comme l'extraction sur
        systèmes de fichiers insensibles à la casse (parité tous OS)."""
        from core.inspector import AttachmentInfo

        base = config(tmp_path, backend="ffmpeg")
        base.sources[0].selected_attachments = [
            AttachmentInfo(index=5, local_index=0, filename="cover.jpg",
                           mimetype="image/jpeg", size_bytes=100, is_attached_pic=True),
            AttachmentInfo(index=6, local_index=1, filename="COVER.JPG",
                           mimetype="image/jpeg", size_bytes=100, is_attached_pic=True),
        ]
        contract = self._contract(base)
        assert contract.attachment_names == ("cover.jpg", "COVER_1.jpg")

    def test_empty_tmdb_filename_uses_download_fallback_name(self, tmp_path: Path) -> None:
        cfg = config(tmp_path, backend="ffmpeg")
        cfg.tmdb_cover = ("https://image.example/cover.jpg", "")

        from core.workflows.remux_plan import plan_remux

        plan = plan_remux(cfg)
        assert plan.output_contract.attachment_names == ("cover.jpg",)
        tmdb_action = next(
            action for action in plan.preparation_actions
            if action.kind == "download_tmdb_cover"
        )
        assert tmdb_action.target_name == "cover.jpg"
