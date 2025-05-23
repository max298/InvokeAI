import math
from typing import Literal, Optional

import torch
from PIL import Image
from transformers import SiglipImageProcessor, SiglipVisionModel

from invokeai.app.invocations.baseinvocation import (
    BaseInvocation,
    BaseInvocationOutput,
    Classification,
    invocation,
    invocation_output,
)
from invokeai.app.invocations.fields import (
    FieldDescriptions,
    FluxReduxConditioningField,
    InputField,
    OutputField,
    TensorField,
    UIType,
)
from invokeai.app.invocations.model import ModelIdentifierField
from invokeai.app.invocations.primitives import ImageField
from invokeai.app.services.model_records.model_records_base import ModelRecordChanges
from invokeai.app.services.shared.invocation_context import InvocationContext
from invokeai.backend.flux.redux.flux_redux_model import FluxReduxModel
from invokeai.backend.model_manager import BaseModelType, ModelType
from invokeai.backend.model_manager.config import AnyModelConfig
from invokeai.backend.model_manager.starter_models import siglip
from invokeai.backend.sig_lip.sig_lip_pipeline import SigLipPipeline
from invokeai.backend.util.devices import TorchDevice


@invocation_output("flux_redux_output")
class FluxReduxOutput(BaseInvocationOutput):
    """The conditioning output of a FLUX Redux invocation."""

    redux_cond: FluxReduxConditioningField = OutputField(
        description=FieldDescriptions.flux_redux_conditioning, title="Conditioning"
    )


DOWNSAMPLING_FUNCTIONS = Literal["nearest", "bilinear", "bicubic", "area", "nearest-exact"]


@invocation(
    "flux_redux",
    title="FLUX Redux",
    tags=["ip_adapter", "control"],
    category="ip_adapter",
    version="2.1.0",
    classification=Classification.Beta,
)
class FluxReduxInvocation(BaseInvocation):
    """Runs a FLUX Redux model to generate a conditioning tensor."""

    image: ImageField = InputField(description="The FLUX Redux image prompt.")
    mask: Optional[TensorField] = InputField(
        default=None,
        description="The bool mask associated with this FLUX Redux image prompt. Excluded regions should be set to "
        "False, included regions should be set to True.",
    )
    redux_model: ModelIdentifierField = InputField(
        description="The FLUX Redux model to use.",
        title="FLUX Redux Model",
        ui_type=UIType.FluxReduxModel,
    )
    downsampling_factor: int = InputField(
        ge=1,
        le=9,
        default=1,
        description="Redux Downsampling Factor (1-9)",
    )
    downsampling_function: DOWNSAMPLING_FUNCTIONS = InputField(
        default="area",
        description="Redux Downsampling Function",
    )
    weight: float = InputField(
        ge=0,
        le=1,
        default=1.0,
        description="Redux weight (0.0-1.0)",
    )

    def invoke(self, context: InvocationContext) -> FluxReduxOutput:
        image = context.images.get_pil(self.image.image_name, "RGB")

        encoded_x = self._siglip_encode(context, image)
        redux_conditioning = self._flux_redux_encode(context, encoded_x)
        if self.downsampling_factor > 1 or self.weight != 1.0:
            redux_conditioning = self._downsample_weight(context, redux_conditioning)

        tensor_name = context.tensors.save(redux_conditioning)
        return FluxReduxOutput(
            redux_cond=FluxReduxConditioningField(conditioning=TensorField(tensor_name=tensor_name), mask=self.mask)
        )

    @torch.no_grad()
    def _downsample_weight(self, context: InvocationContext, redux_conditioning: torch.Tensor) -> torch.Tensor:
        # Downsampling derived from https://github.com/kaibioinfo/ComfyUI_AdvancedRefluxControl
        (b, t, h) = redux_conditioning.shape
        m = int(math.sqrt(t))
        if self.downsampling_factor > 1:
            redux_conditioning = redux_conditioning.view(b, m, m, h)
            redux_conditioning = torch.nn.functional.interpolate(
                redux_conditioning.transpose(1, -1),
                size=(m // self.downsampling_factor, m // self.downsampling_factor),
                mode=self.downsampling_function,
            )
            redux_conditioning = redux_conditioning.transpose(1, -1).reshape(b, -1, h)
        if self.weight != 1.0:
            redux_conditioning = redux_conditioning * self.weight * self.weight
        return redux_conditioning

    @torch.no_grad()
    def _siglip_encode(self, context: InvocationContext, image: Image.Image) -> torch.Tensor:
        siglip_model_config = self._get_siglip_model(context)
        with context.models.load(siglip_model_config.key).model_on_device() as (_, model):
            assert isinstance(model, SiglipVisionModel)

            model_abs_path = context.models.get_absolute_path(siglip_model_config)
            processor = SiglipImageProcessor.from_pretrained(model_abs_path, local_files_only=True)
            assert isinstance(processor, SiglipImageProcessor)

            siglip_pipeline = SigLipPipeline(processor, model)
            return siglip_pipeline.encode_image(
                x=image, device=TorchDevice.choose_torch_device(), dtype=TorchDevice.choose_torch_dtype()
            )

    @torch.no_grad()
    def _flux_redux_encode(self, context: InvocationContext, encoded_x: torch.Tensor) -> torch.Tensor:
        with context.models.load(self.redux_model).model_on_device() as (_, flux_redux):
            assert isinstance(flux_redux, FluxReduxModel)
            dtype = next(flux_redux.parameters()).dtype
            encoded_x = encoded_x.to(dtype=dtype)
            return flux_redux(encoded_x)

    def _get_siglip_model(self, context: InvocationContext) -> AnyModelConfig:
        siglip_models = context.models.search_by_attrs(name=siglip.name, base=BaseModelType.Any, type=ModelType.SigLIP)

        if not len(siglip_models) > 0:
            context.logger.warning(
                f"The SigLIP model required by FLUX Redux ({siglip.name}) is not installed. Downloading and installing now. This may take a while."
            )

            # TODO(psyche): Can the probe reliably determine the type of the model? Just hardcoding it bc I don't want to experiment now
            config_overrides = ModelRecordChanges(name=siglip.name, type=ModelType.SigLIP)

            # Queue the job
            job = context._services.model_manager.install.heuristic_import(siglip.source, config=config_overrides)

            # Wait for up to 10 minutes - model is ~3.5GB
            context._services.model_manager.install.wait_for_job(job, timeout=600)

            siglip_models = context.models.search_by_attrs(
                name=siglip.name,
                base=BaseModelType.Any,
                type=ModelType.SigLIP,
            )

            if len(siglip_models) == 0:
                context.logger.error("Error while fetching SigLIP for FLUX Redux")
                assert len(siglip_models) == 1

        return siglip_models[0]
