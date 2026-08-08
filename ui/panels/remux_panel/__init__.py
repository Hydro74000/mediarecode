"""ui/panels/remux_panel — package modulaire du panneau Remux."""

from ui.panels.remux_panel.models import (
    SourceFile,
    _FILE_BAR_H,
    _FILE_PH_H,
    _FILE_ROW_H,
    _TRACK_INFO_DISABLED_LABEL_ROLE,
    _TRACK_INFO_OFFSET_COLOR,
    _TRACK_INFO_OFFSET_NEG_COLOR,
    _TRACK_INFO_OFFSET_POS_COLOR,
    _TRACK_INFO_OFFSET_VALUE_ROLE,
    _TRACK_INFO_SYNC_LABEL_ROLE,
    _normalize_tmdb_manual_title_suggestion,
    _pick_file_color,
)
from ui.panels.remux_panel.panel import RemuxPanel
from ui.panels.remux_panel.widgets.attachments import _AttachmentItemWidget, _AttachmentPanel
from ui.panels.remux_panel.widgets.file_list import _FileListWidget
from ui.panels.remux_panel.widgets.track_table import _TrackInfoDelegate, _TrackTable

__all__ = [
    "RemuxPanel",
    "SourceFile",
    "_AttachmentItemWidget",
    "_AttachmentPanel",
    "_FileListWidget",
    "_TrackInfoDelegate",
    "_TrackTable",
    "_FILE_BAR_H",
    "_FILE_PH_H",
    "_FILE_ROW_H",
    "_TRACK_INFO_DISABLED_LABEL_ROLE",
    "_TRACK_INFO_OFFSET_COLOR",
    "_TRACK_INFO_OFFSET_NEG_COLOR",
    "_TRACK_INFO_OFFSET_POS_COLOR",
    "_TRACK_INFO_OFFSET_VALUE_ROLE",
    "_TRACK_INFO_SYNC_LABEL_ROLE",
    "_normalize_tmdb_manual_title_suggestion",
    "_pick_file_color",
]
