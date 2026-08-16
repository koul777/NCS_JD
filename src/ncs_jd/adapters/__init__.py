"""Infrastructure adapters for NCS JD."""

from ncs_jd.adapters.fake_ncs_source import FakeNcsSourceAdapter
from ncs_jd.adapters.kordoc_hwpx_renderer import KordocHwpxRenderer
from ncs_jd.adapters.kordoc_parser import KordocDocumentParser
from ncs_jd.adapters.ncs_mcp_source import NcsMcpSourceAdapter

__all__ = [
    "FakeNcsSourceAdapter",
    "KordocDocumentParser",
    "KordocHwpxRenderer",
    "NcsMcpSourceAdapter",
]
