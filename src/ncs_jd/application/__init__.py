"""Application ports and use-case contracts."""

from ncs_jd.application.document_parser import DocumentParserPort
from ncs_jd.application.document_renderer import DocumentRendererPort
from ncs_jd.application.drafting_workflow import (
    ConfirmedDraftRequest,
    DraftGenerationResult,
    DraftingWorkflow,
)
from ncs_jd.application.job_profile_assembler import (
    JobProfileAssembler,
    JobProfileAssemblyRequest,
    OrganizationInput,
)
from ncs_jd.application.ncs_source import NcsSourcePort

__all__ = [
    "ConfirmedDraftRequest",
    "DocumentParserPort",
    "DocumentRendererPort",
    "DraftGenerationResult",
    "DraftingWorkflow",
    "JobProfileAssembler",
    "JobProfileAssemblyRequest",
    "NcsSourcePort",
    "OrganizationInput",
]
