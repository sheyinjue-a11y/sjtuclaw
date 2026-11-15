"""Composition root that wires runtime ports to concrete infrastructure."""

from __future__ import annotations

from dataclasses import dataclass, replace

from sjtuclaw.config import ProviderConfiguration, ProviderKind, Settings
from sjtuclaw.infrastructure.llm import (
    AnthropicLLMClient,
    DeepSeekLLMClient,
    OpenAICompatibleLLMClient,
)
from sjtuclaw.infrastructure.persistence import (
    JsonAttachmentStore,
    JsonMemoryPolicyStore,
    JsonMemoryStore,
    JsonSessionStore,
    JsonSkillSnapshotStore,
    JsonTaskStore,
)
from sjtuclaw.infrastructure.runtime import SystemClock, UuidIdGenerator
from sjtuclaw.infrastructure.tokenizers import (
    ApproximateTokenCounter,
    DeepSeekTokenCounter,
)
from sjtuclaw.infrastructure.tools.advanced import (
    CopyAttachmentToWorkspaceTool,
    CreateDownloadTool,
    NewShellTool,
    RunCommandTool,
    WriteFileTool,
)
from sjtuclaw.infrastructure.tools.downloads import DownloadSnapshotService
from sjtuclaw.infrastructure.tools.readonly import create_read_only_tools
from sjtuclaw.infrastructure.tools.registry import ToolRegistry
from sjtuclaw.infrastructure.tools.shells import PersistentShellManager
from sjtuclaw.infrastructure.tools.skill_selection import SelectSkillTool
from sjtuclaw.infrastructure.tools.skills import ReadSkillResourceTool
from sjtuclaw.models.llm import LLMToolMode
from sjtuclaw.ports.token_counter import TokenCounter
from sjtuclaw.runtime import (
    ArchiveService,
    AttachmentService,
    CommandService,
    CompactionService,
    ContextBuilder,
    MemoryCapture,
    MemoryCoordinator,
    MemoryPolicyService,
    MemoryService,
    PermissionPolicy,
    PromptConfigurationService,
    RuntimeHost,
    SchedulerService,
    SessionCoordinator,
    SessionService,
    SkillService,
    WorkspaceService,
)
from sjtuclaw.runtime.memory_extractor import MemoryExtractor
from sjtuclaw.runtime.memory_retriever import LocalMemoryRetriever
from sjtuclaw.runtime.provider_runtime import (
    ClosableStreamingLLMClient,
    ProviderRuntime,
)
from sjtuclaw.runtime.session_title import SessionTitleGenerator


@dataclass(frozen=True, slots=True)
class RuntimeApplication:
    runtime: RuntimeHost
    session_service: SessionService
    attachment_service: AttachmentService
    scheduler: SchedulerService
    archive_service: ArchiveService
    workspace_service: WorkspaceService
    downloads: DownloadSnapshotService
    shells: PersistentShellManager
    skills: SkillService
    compaction: CompactionService
    memory_service: MemoryService
    memory_policy: MemoryPolicyService
    prompts: PromptConfigurationService
    commands: CommandService
    initial_session_id: str
    llm: ProviderRuntime

    async def aclose(self) -> None:
        await self.scheduler.aclose()
        await self.runtime.aclose()
        await self.shells.aclose()
        self.downloads.aclose()
        await self.llm.aclose()


def build_cli_application(
    settings: Settings,
    system_prompt: str,
    soul: str,
    compact_prompt: str,
    *,
    provider_configuration: ProviderConfiguration | None = None,
) -> RuntimeApplication:
    clock = SystemClock()
    ids = UuidIdGenerator()
    sessions = JsonSessionStore(settings.data_dir)
    memory = JsonMemoryStore(settings.data_dir)
    attachments = JsonAttachmentStore(
        settings.data_dir,
        pdf_timeout_seconds=settings.attachment_process_timeout_seconds,
    )
    skills = SkillService(
        source_root=settings.skill_root,
        snapshots=JsonSkillSnapshotStore(settings.data_dir),
        clock=clock,
    )
    if not skills.list() or not {"pdf", "presentations"}.issubset(
        {manifest.skill_id for manifest in skills.list()}
    ):
        skills.refresh()
    coordinator = SessionCoordinator()
    session_service = SessionService(
        sessions=sessions,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    attachment_service = AttachmentService(
        sessions=sessions,
        attachments=attachments,
        coordinator=coordinator,
    )
    existing_sessions = session_service.list()
    initial_session = existing_sessions[0] if existing_sessions else session_service.create()
    providers = _build_provider_runtime(
        settings,
        ids=ids,
        initial=provider_configuration,
    )
    memory_coordinator = MemoryCoordinator()
    memory_service = MemoryService(
        memory,
        memory_coordinator,
        clock=clock.now,
        new_id=lambda: ids.new_id("memory"),
    )
    memory_policy = MemoryPolicyService(
        JsonMemoryPolicyStore(settings.data_dir),
        clock=clock.now,
    )
    memory_retriever = LocalMemoryRetriever(
        memory,
        providers,
        limit=settings.memory_retrieval_limit,
        max_tokens=settings.memory_context_max_tokens,
    )
    memory_capture = MemoryCapture(
        policy=memory_policy,
        retriever=memory_retriever,
        extractor=MemoryExtractor(llm=providers, clock=clock.now),
        service=memory_service,
    )
    prompts = PromptConfigurationService(
        root=settings.data_dir / "prompts",
        default_system_prompt=system_prompt,
        default_soul=soul,
    )
    context_builder = ContextBuilder(
        prompt_configuration=prompts,
        memory_retriever=memory_retriever,
        attachments=attachments,
        skills=skills,
    )
    downloads = DownloadSnapshotService(root=settings.data_dir, clock=clock, ids=ids)
    shells = PersistentShellManager(ids=ids)
    tool_registry = ToolRegistry(
        (
            *create_read_only_tools(
                settings.tool_root,
                now=clock.now,
                max_file_bytes=settings.tool_max_file_bytes,
                attachments=attachments,
            ),
            CopyAttachmentToWorkspaceTool(attachments=attachments, originals=attachments),
            WriteFileTool(),
            CreateDownloadTool(publisher=downloads),
            NewShellTool(manager=shells),
            RunCommandTool(manager=shells),
            ReadSkillResourceTool(skills=skills),
            SelectSkillTool(skills=skills),
        )
    )
    permission = PermissionPolicy(
        tool_registry.definitions(),
        allowed_roots=(settings.tool_root,),
    )
    tool_mode = LLMToolMode(settings.tool_mode)
    compaction = CompactionService(
        llm=providers,
        sessions=sessions,
        context_builder=context_builder,
        token_counter=providers,
        coordinator=coordinator,
        clock=clock,
        compact_prompt=compact_prompt,
        budget=settings.context_budget_tokens,
        trigger_ratio=settings.compaction_trigger_ratio,
        target_ratio=settings.compaction_target_ratio,
        keep_recent_turns=settings.compaction_keep_recent_turns,
        output_reserve=settings.llm_output_reserve_tokens,
        trigger_tokens=settings.compaction_trigger_tokens,
        target_tokens=settings.compaction_target_tokens,
        token_counter_mode=lambda: providers.token_counter_mode,
        tools=tool_registry.definitions(),
        tool_mode=tool_mode,
    )
    commands = CommandService(
        sessions=session_service,
        memories=memory_service,
        compaction=compaction,
        skills=skills,
    )
    runtime = RuntimeHost(
        llm=providers,
        sessions=sessions,
        context_builder=context_builder,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
        compaction=compaction,
        tools=tool_registry,
        permission=permission,
        tool_mode=tool_mode,
        max_steps=settings.agent_max_steps,
        repeat_limit=settings.agent_repeat_call_limit,
        attachments=attachments,
        skills=skills,
        session_titles=SessionTitleGenerator(llm=providers, clock=clock),
        provider_runtime=providers,
        memory_capture=memory_capture,
    )
    scheduler = SchedulerService(
        tasks=JsonTaskStore(settings.data_dir),
        runtime=runtime,
        sessions=sessions,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    workspace_service = WorkspaceService(
        sessions=sessions,
        coordinator=coordinator,
        clock=clock,
        ids=ids,
    )
    archive_service = ArchiveService(
        sessions=sessions,
        coordinator=coordinator,
        runtime=runtime,
        scheduler=scheduler,
        attachments=attachments,
        downloads=downloads,
        clock=clock,
    )
    return RuntimeApplication(
        runtime=runtime,
        session_service=session_service,
        attachment_service=attachment_service,
        scheduler=scheduler,
        archive_service=archive_service,
        workspace_service=workspace_service,
        downloads=downloads,
        shells=shells,
        skills=skills,
        compaction=compaction,
        memory_service=memory_service,
        memory_policy=memory_policy,
        prompts=prompts,
        commands=commands,
        initial_session_id=initial_session.session_id,
        llm=providers,
    )


def _build_provider_runtime(
    settings: Settings,
    *,
    ids: UuidIdGenerator,
    initial: ProviderConfiguration | None,
) -> ProviderRuntime:
    configuration = initial or ProviderConfiguration(
        provider=ProviderKind.DEEPSEEK,
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
    )
    return ProviderRuntime(
        configuration,
        create_client=lambda current: _build_provider_client(settings, current, ids),
        create_token_counter=lambda current: _build_token_counter(settings, current),
    )


def _build_provider_client(
    settings: Settings,
    configuration: ProviderConfiguration,
    ids: UuidIdGenerator,
) -> ClosableStreamingLLMClient:
    if configuration.provider is ProviderKind.DEEPSEEK:
        return DeepSeekLLMClient(
            replace(
                settings,
                api_key=configuration.api_key,
                base_url=configuration.base_url,
                model=configuration.model,
            ),
            thinking_enabled=configuration.thinking_enabled,
            ids=ids,
        )
    if configuration.provider is ProviderKind.OPENAI_COMPATIBLE:
        return OpenAICompatibleLLMClient(configuration, ids=ids)
    return AnthropicLLMClient(configuration, ids=ids)


def _build_token_counter(
    settings: Settings,
    configuration: ProviderConfiguration,
) -> TokenCounter:
    if configuration.provider is ProviderKind.DEEPSEEK:
        return DeepSeekTokenCounter(settings.tokenizer_path)
    return ApproximateTokenCounter()
