"""FastAPI application factory for the backend-independent UI skeleton."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ncs_jd import __version__
from ncs_jd.application.template_mapping import TemplateInspectorPort
from ncs_jd.web.api import create_api_router

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ncs_jd.application.agent_drafting import AgentDraftPort
    from ncs_jd.application.document_parser import DocumentParserPort
    from ncs_jd.application.document_renderer import DocumentRendererPort
    from ncs_jd.application.drafting_workflow import DraftingWorkflow
    from ncs_jd.application.llm_scope_selection import ScopeSelectorPort
    from ncs_jd.application.ncs_source import NcsSourcePort
    from ncs_jd.application.template_mapping import TemplateMappingPort
    from ncs_jd.application.notice_jd_case_library import NoticeJDCase


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_KORDOC_VERSION = "4.2.9"
_KORDOC_SELF_CHECK_TIMEOUT_SECONDS = 8.0
_KORDOC_BRIDGES = (
    _PROJECT_ROOT / "scripts" / "kordoc_bridge.mjs",
    _PROJECT_ROOT / "scripts" / "kordoc_hwpx_bridge.mjs",
)
_DEFAULT_CASE_LIBRARY_PATH = _PROJECT_ROOT / "build" / "alio_notice_jd_case_library.json"


@dataclass(frozen=True, slots=True)
class KordocReadiness:
    """Sanitized result of the local Node/Kordoc capability self-check."""

    ready: bool
    reason: str = "ready"


def _check_kordoc_readiness(
    *,
    node_executable: str = "node",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> KordocReadiness:
    """Verify the exact Kordoc runtime without processing an uploaded document."""

    if any(not bridge.is_file() for bridge in _KORDOC_BRIDGES):
        return KordocReadiness(False, "kordoc_bridge_missing")

    required_exports = (
        "blocksToMarkdown",
        "detectFormat",
        "detectZipFormat",
        "extractFormSchema",
        "fillForm",
        "fillFormFields",
        "markdownToHwpx",
        "parse",
        "parseHwpx",
        "renderHwpxToSvg",
        "validateHwpx",
    )
    export_list = ",".join(f'"{name}"' for name in required_exports)
    script = (
        'const k=await import("kordoc");'
        f'if(k.VERSION!=="{_KORDOC_VERSION}")process.exit(2);'
        f'if([{export_list}].some((n)=>typeof k[n]!=="function"))process.exit(3);'
    )
    try:
        result = runner(
            (node_executable, "--input-type=module", "--eval", script),
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_KORDOC_SELF_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return KordocReadiness(False, "node_unavailable")
    except subprocess.TimeoutExpired:
        return KordocReadiness(False, "kordoc_self_check_timeout")
    except OSError:
        return KordocReadiness(False, "kordoc_self_check_unavailable")

    if result.returncode == 2:
        return KordocReadiness(False, "kordoc_version_mismatch")
    if result.returncode == 3:
        return KordocReadiness(False, "kordoc_capability_missing")
    if result.returncode != 0:
        return KordocReadiness(False, "kordoc_self_check_failed")
    return KordocReadiness(True)


def _resolve_case_library_path(path: str | Path | None) -> Path:
    override = os.environ.get("NCS_JD_CASE_LIBRARY_PATH")
    if override:
        return Path(override).expanduser()
    if path is not None:
        return Path(path).expanduser()
    return _DEFAULT_CASE_LIBRARY_PATH


def _load_case_library(
    path: str | Path | None,
) -> tuple["NoticeJDCase", ...]:
    try:
        from ncs_jd.application.notice_jd_case_library import load_case_library
    except Exception:
        return ()

    return load_case_library(_resolve_case_library_path(path))

def _ui_contract(
    *,
    backend_connected: bool = False,
    providers: tuple[str, ...] = ("off",),
) -> dict[str, Any]:
    """Return the small public contract used by the one-action UI."""

    return {
        "product": "NCS JD",
        "version": "simple-generator-v2",
        "document_status": "draft",
        "backend_connected": backend_connected,
        "local_bind": {"host": DEFAULT_HOST, "port": DEFAULT_PORT},
        "inputs": [
            {
                "id": "announcement",
                "label": "채용 공고문",
                "required": True,
                "formats": ["pdf", "hwp", "hwpx", "docx", "txt"],
            },
            {
                "id": "announcement_text",
                "label": "붙여넣은 채용 공고문",
                "required": False,
                "formats": ["text"],
            },
            {
                "id": "job_title",
                "label": "조직 내 직무명",
                "required": False,
                "formats": ["text"],
            },
            {
                "id": "template",
                "label": "NCS 직무기술서 양식",
                "required": False,
                "formats": ["pdf", "hwp", "hwpx"],
            },
        ],
        "generation_mode": "deterministic_with_optional_template_mapping",
        # Every AI path authenticates through an official CLI's own subscription
        # login, so the app never asks a person for a key or token.
        "external_ai_required": False,
        "secret_inputs": [],
        "providers": list(providers),
        "output": "hwpx",
        "notice": "검토가 필요한 초안이며 채용 결정 또는 공식 자격 판정 자료가 아닙니다.",
    }


def create_app(
    *,
    template_directory: str | Path | None = None,
    static_directory: str | Path | None = None,
    workflow: DraftingWorkflow | None = None,
    ncs_source: NcsSourcePort | None = None,
    renderer: DocumentRendererPort | None = None,
    cli_template_mapper: TemplateMappingPort | None = None,
    scope_selectors: Mapping[str, ScopeSelectorPort] | None = None,
    agent_runners: Mapping[str, AgentDraftPort] | None = None,
    case_library: Sequence[NoticeJDCase] | None = None,
    case_library_path: str | Path | None = None,
    case_library_top_k: int = 5,
    case_library_min_confidence: float = 0.4,
    case_library_per_label_examples: int = 1,
    kordoc_readiness: KordocReadiness | None = None,
) -> FastAPI:
    """Create the local-only UI, optionally with injected drafting backends."""

    dependencies = (workflow, ncs_source, renderer)
    if any(dependency is not None for dependency in dependencies) and not all(
        dependency is not None for dependency in dependencies
    ):
        raise ValueError("workflow, ncs_source, and renderer must be supplied together")
    backend_configured = all(dependency is not None for dependency in dependencies)
    if cli_template_mapper is not None and not backend_configured:
        raise ValueError("cli_template_mapper requires all drafting backends")
    if scope_selectors and not backend_configured:
        raise ValueError("scope_selectors requires all drafting backends")
    if agent_runners and not backend_configured:
        raise ValueError("agent_runners requires all drafting backends")
    if kordoc_readiness is not None and not backend_configured:
        raise ValueError("kordoc_readiness requires all drafting backends")
    resolved_case_library = (
        tuple(case_library) if case_library is not None else _load_case_library(case_library_path)
    )
    backend_connected = backend_configured and (
        kordoc_readiness is None or kordoc_readiness.ready
    )
    # Only advertise a CLI the UI can actually run end to end.  A provider that
    # can select scope but has no agent runner would otherwise appear as a
    # choosable engine and then fail at the point of use.
    advertised_providers = ("off",) + tuple(
        name
        for name in ("claude", "codex")
        if name in (scope_selectors or {}) and name in (agent_runners or {})
    )

    template_path = Path(template_directory) if template_directory else _PROJECT_ROOT / "templates"
    static_path = Path(static_directory) if static_directory else _PROJECT_ROOT / "static"

    app = FastAPI(
        title="NCS JD",
        description="NCS 근거 기반 직무기술서·직무명세서 로컬 초안 UI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bind_host = DEFAULT_HOST
    app.state.bind_port = DEFAULT_PORT
    app.state.backend_connected = backend_connected
    app.state.kordoc_readiness = kordoc_readiness

    if backend_connected:
        app.include_router(
            create_api_router(
                workflow=workflow,
                ncs_source=ncs_source,
                renderer=renderer,
                cli_template_mapper=cli_template_mapper,
                scope_selectors=scope_selectors,
                agent_runners=agent_runners,
                # The Kordoc renderer already owns form inspection; the agent
                # path needs it to let an institution form pick the labels.
                # Injected test renderers need not provide it.
                template_inspector=(
                    renderer if isinstance(renderer, TemplateInspectorPort) else None
                ),
                case_library=resolved_case_library,
                case_library_top_k=case_library_top_k,
                case_library_min_confidence=case_library_min_confidence,
                case_library_per_label_examples=case_library_per_label_examples,
            )
        )

    templates = Jinja2Templates(directory=str(template_path))
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def workspace(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"ui_contract": _ui_contract(backend_connected=backend_connected, providers=advertised_providers)},
        )

    @app.get("/health", tags=["system"])
    async def health(response: Response) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "ok" if not backend_configured or backend_connected else "degraded",
            "service": "ncs-jd-web",
            "bind_host": DEFAULT_HOST,
            "backend_connected": backend_connected,
        }
        if backend_configured and not backend_connected:
            response.status_code = 503
            payload["backend_readiness"] = (
                kordoc_readiness.reason if kordoc_readiness is not None else "not_ready"
            )
        return payload

    @app.get("/api/ui-contract", tags=["ui"])
    async def ui_contract() -> dict[str, Any]:
        return _ui_contract(backend_connected=backend_connected, providers=advertised_providers)

    return app


def _build_agent_runners() -> dict[str, AgentDraftPort]:
    """Point each agent CLI at the same sidecar the launcher would start.

    The agent runs its own stdio copy of the sidecar, so it needs the executable
    and serving database directly.  Source checkouts resolve to a Python
    interpreter instead, which needs the module arguments and ``PYTHONPATH`` the
    HTTP launcher script sets.  An empty mapping simply leaves the agent route
    disabled; the deterministic path is unaffected.

    Both CLIs get a runner regardless of whether their executable is present --
    the runner resolves that at run time and reports it as a progress failure,
    which is a far clearer answer than an engine that silently disappears from
    the picker.
    """

    from ncs_jd.adapters.cli_agent_runner import CliAgentDraftRunner, NcsMcpServerSpec
    from ncs_jd.application.llm_rewriter import LlmProvider
    from ncs_jd.desktop_launcher import find_ncs_mcp_target, resolve_serving_database

    target = find_ncs_mcp_target()
    if target is None:
        return {}
    database = resolve_serving_database(target)
    if database is None:
        return {}
    if target.mode == "portable":
        spec = NcsMcpServerSpec(
            command=str(target.executable),
            database_path=str(database),
        )
    elif target.mode == "source-python":
        spec = NcsMcpServerSpec(
            command=str(target.executable),
            database_path=str(database),
            args=("-m", "ncs_mcp.server"),
            extra_env=(("PYTHONPATH", str(target.root / "src")),),
        )
    else:
        return {}
    runners: dict[str, AgentDraftPort] = {}
    for provider in (LlmProvider.CLAUDE, LlmProvider.CODEX):
        try:
            runners[provider.value] = CliAgentDraftRunner(spec, provider=provider)
        except ValueError:
            continue
    return runners


def create_connected_app(
    *,
    kordoc_readiness: KordocReadiness | None = None,
    parser: DocumentParserPort | None = None,
    renderer: DocumentRendererPort | None = None,
    template_directory: str | Path | None = None,
    static_directory: str | Path | None = None,
    case_library: Sequence[NoticeJDCase] | None = None,
    case_library_path: str | Path | None = None,
    case_library_top_k: int = 5,
    case_library_min_confidence: float = 0.4,
    case_library_per_label_examples: int = 1,
) -> FastAPI:
    """Compose the production-local Kordoc, NCS MCP, and HWPX adapters."""

    from ncs_jd.adapters.cli_llm import CliLlmAdapter, CliScopeSelector
    from ncs_jd.adapters.cli_template_mapper import CliTemplateMapper
    from ncs_jd.adapters.kordoc_hwpx_renderer import KordocHwpxRenderer
    from ncs_jd.adapters.kordoc_parser import KordocDocumentParser
    from ncs_jd.adapters.ncs_mcp_source import NcsMcpSourceAdapter
    from ncs_jd.application.drafting_workflow import DraftingWorkflow
    from ncs_jd.application.llm_rewriter import LlmProvider
    from ncs_jd.web.api import create_llm_api_router

    parser = parser or KordocDocumentParser()
    ncs_source = NcsMcpSourceAdapter()
    renderer = renderer or KordocHwpxRenderer()
    workflow = DraftingWorkflow(parser, ncs_source)
    readiness = kordoc_readiness or _check_kordoc_readiness()

    # The official CLIs authenticate through their own subscription login, so
    # registering them here adds no key handling.  An uninstalled or logged-out
    # provider simply falls back to the deterministic plan.
    llm_cli = CliLlmAdapter()
    cli_template_mapper = CliTemplateMapper(renderer, llm_cli, provider=LlmProvider.CLAUDE)
    scope_selectors = {
        LlmProvider.CLAUDE.value: CliScopeSelector(llm_cli, LlmProvider.CLAUDE),
        LlmProvider.CODEX.value: CliScopeSelector(llm_cli, LlmProvider.CODEX),
    }
    app = create_app(
        template_directory=template_directory,
        static_directory=static_directory,
        workflow=workflow,
        ncs_source=ncs_source,
        renderer=renderer,
        cli_template_mapper=cli_template_mapper,
        scope_selectors=scope_selectors,
        agent_runners=_build_agent_runners(),
        case_library=case_library,
        case_library_path=case_library_path,
        case_library_top_k=case_library_top_k,
        case_library_min_confidence=case_library_min_confidence,
        case_library_per_label_examples=case_library_per_label_examples,
        kordoc_readiness=readiness,
    )
    app.include_router(create_llm_api_router(llm_cli=llm_cli))
    return app
