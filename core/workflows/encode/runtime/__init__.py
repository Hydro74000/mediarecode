"""Runtime runners for the encode workflow.

## Conventions runtime

### Plage `log_step`
- `1` a `4` restent reserves a la preparation de haut niveau dans
  `EncodeWorkflow._run_with_preparation`.
- Les runners runtime commencent a `5` et ne doivent pas reutiliser `1..4`.

### Ranges par runner
- `DirectOutputRunner`:
  - `5`: construction de commande
  - `6`: preparation sync/remap
  - `7`: execution ffmpeg (single-pass ou two-pass)
- `MetadataInjectRunner`:
  - `5`: extraction metadonnees dynamiques (DoVi/HDR10+)
  - `6`: encodage video seule
  - `7`: injection HDR10+ / DoVi
  - `8`: encapsulation timeline de la video injectee
  - `9`: reconstruction finale MKV
- `MultiVideoPipelineRunner`:
  - preparation des pistes faite via logs/progress dedies
  - `5`: reconstruction finale multi-pistes video

### Contrats callbacks
- `check_cancelled(signals)` doit lever `TaskCancelledError` quand necessaire.
- `build_encode_plan(config)` doit retourner un plan stable reutilisable sur tout
  le pipeline du runner.
- `run_cmd(...)` est la seule porte d'execution process et doit relayer la
  progression vers `signals.progress`.
- Les callbacks de mapping/sync (`prepare_multisource_sync`,
  `append_sync_inputs`, `append_offset_aux_inputs`) doivent preserver les
  index d'inputs pour garder les `-map` deterministes.
"""

from .attachment_preparation import (
    AttachmentPreparationService,
    AttachmentPreparationServiceCallbacks,
    default_attachment_filename,
    extract_attached_pic,
    probe_attachment_stream,
    unique_attachment_path,
)
from .bindings import SignalBindingService, SignalBindingServiceCallbacks
from .direct_output import DirectOutputRunner, DirectOutputRunnerCallbacks
from .dynamic_hdr import DynamicHdrConfigNormalizer, DynamicHdrNormalizerCallbacks
from .hdr_metadata import HdrMetadataProbeService
from .metadata_inject import MetadataInjectRunner, MetadataInjectRunnerCallbacks
from .multi_video import MultiVideoPipelineRunner, MultiVideoPipelineRunnerCallbacks
from .mux_assembly import (
    EncodeFinalMuxBuilder,
    EncodeFinalMuxBuilderCallbacks,
    EncodeStreamMappingService,
    EncodeStreamMappingCallbacks,
    TrackMetadataArgsBuilder,
    TrackMetadataArgsBuilderCallbacks,
)
from .multisource_sync import EncodeMultisourceSyncService, EncodeMultisourceSyncCallbacks
from .nvencc_execution import (
    NvenccAssetPreparationService,
    NvenccAssetPreparationCallbacks,
    NvenccDirectOutputRunner,
    NvenccDirectOutputRunnerCallbacks,
    NvenccPipeExecutor,
    NvenccRuntimeRemuxBuilder,
    NvenccRuntimeRemuxBuilderCallbacks,
)
from .nvencc_routing import (
    NvenccInputRouter,
    NvenccInputRouting,
    NvenccRoutingCallbacks,
)
from .preparation import EncodePreparationRunner, EncodePreparationRunnerCallbacks
from .static_hdr_estimator import StaticHdrEstimate, StaticHdrEstimateService
from .storage_guard import (
    ensure_inject_storage_available,
    estimate_duration_seconds,
    estimate_inject_storage_requirements,
    estimate_inject_video_bytes,
    format_bytes,
)
from .video_preparation import (
    TwoPassLogCleanupService,
    TwoPassRunner,
    TwoPassRunnerCallbacks,
    VideoOnlyCommandBuilder,
    VideoOnlyCommandBuilderCallbacks,
    VideoPreparationPolicyCallbacks,
    VideoPreparationPolicyService,
)

__all__ = [
    "AttachmentPreparationService",
    "AttachmentPreparationServiceCallbacks",
    "SignalBindingService",
    "SignalBindingServiceCallbacks",
    "DirectOutputRunner",
    "DirectOutputRunnerCallbacks",
    "DynamicHdrConfigNormalizer",
    "DynamicHdrNormalizerCallbacks",
    "HdrMetadataProbeService",
    "MetadataInjectRunner",
    "MetadataInjectRunnerCallbacks",
    "MultiVideoPipelineRunner",
    "MultiVideoPipelineRunnerCallbacks",
    "EncodeFinalMuxBuilder",
    "EncodeFinalMuxBuilderCallbacks",
    "EncodeStreamMappingService",
    "EncodeStreamMappingCallbacks",
    "TrackMetadataArgsBuilder",
    "TrackMetadataArgsBuilderCallbacks",
    "EncodeMultisourceSyncService",
    "EncodeMultisourceSyncCallbacks",
    "NvenccAssetPreparationService",
    "NvenccAssetPreparationCallbacks",
    "NvenccDirectOutputRunner",
    "NvenccDirectOutputRunnerCallbacks",
    "NvenccPipeExecutor",
    "NvenccRuntimeRemuxBuilder",
    "NvenccRuntimeRemuxBuilderCallbacks",
    "NvenccInputRouter",
    "NvenccInputRouting",
    "NvenccRoutingCallbacks",
    "EncodePreparationRunner",
    "EncodePreparationRunnerCallbacks",
    "StaticHdrEstimate",
    "StaticHdrEstimateService",
    "TwoPassLogCleanupService",
    "TwoPassRunner",
    "TwoPassRunnerCallbacks",
    "VideoOnlyCommandBuilder",
    "VideoOnlyCommandBuilderCallbacks",
    "VideoPreparationPolicyCallbacks",
    "VideoPreparationPolicyService",
    "default_attachment_filename",
    "ensure_inject_storage_available",
    "estimate_duration_seconds",
    "estimate_inject_storage_requirements",
    "estimate_inject_video_bytes",
    "extract_attached_pic",
    "format_bytes",
    "probe_attachment_stream",
    "unique_attachment_path",
]
