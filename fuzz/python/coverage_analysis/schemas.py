"""Typed contracts for coverage-analysis tool calls."""

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ListPartiesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PartyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    description: str
    alias: list[str]
    binding: Literal["sut", "generator", "unavailable"] | None


class ListPartiesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parties: list[PartyInfo]

# Input models preserve omission: handlers receive only explicitly supplied keys,
# and apply their existing defaults themselves.
class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyInput(ToolInput):
    pass


class RequestInput(ToolInput):
    request_id: str = Field(description="Registered analysis request ID.")


class OptionalRequestInput(ToolInput):
    request_id: str | None = Field(default=None, description="Registered analysis request ID, when needed.")


class PageInput(OptionalRequestInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class CoverageInput(RequestInput):
    snapshot_dir: str


class CfgInput(ToolInput):
    cfg_dir: str


class CallgraphInput(ToolInput):
    callgraph_dot: str


class ProtocolInput(ToolInput):
    protocol_path: str
    checkpoint_path: str | None = None


class HandlersInput(ToolInput):
    handlers_path: str


class HandlerEntryInput(ToolInput):
    symbol: str
    location: str = ""
    evidence: str = ""
    uncertain: bool = False


class HandlerMappingInput(ToolInput):
    message: str
    handlers: list[HandlerEntryInput]
    step_names: list[str]
    reason: str | None = None


class WriteHandlersInput(ToolInput):
    output_dir: str
    session: str
    mappings: list[HandlerMappingInput]
    load: bool = True


class ModuleInput(PageInput):
    path: str | None = None


class FileInput(PageInput):
    file: str | None = None


class HandlerCoverageInput(PageInput):
    message: str | None = None
    handler_id: str | None = None
    sort: Literal["missed", "name"] | None = None


class FunctionCoverageInput(PageInput):
    function: str | None = None
    sort: Literal["missed", "name"] | None = None


class ReachableInput(OptionalRequestInput):
    function: str
    include_self: bool = False
    limit: int = Field(default=500, ge=1, le=2000)


class ReachingInput(OptionalRequestInput):
    function: str


class DescribeHandlerInput(ToolInput):
    handler_id: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=50)
    source_lines: int = Field(default=60, ge=1, le=100)


class DescribeTraceInput(ToolInput):
    steps: list[str] = Field(min_length=1, max_length=50)
    dependency_offset: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class SourceCoverageInput(OptionalRequestInput):
    function: str | None = None
    file: str | None = None
    around_line: int | None = Field(default=None, ge=1)
    start_line: int | None = None
    end_line: int | None = None
    max_lines: int | None = Field(default=None, ge=1, le=200)


class ProtocolStepsInput(PageInput):
    message: str | None = None
    party: str | None = None


class StepDependenciesInput(RequestInput):
    step: str
    direction: Literal["before", "after", "both"] = "both"
    trace_steps: list[str] | None = None


class ListMessagesInput(ToolInput):
    direction: Literal["sut_inbound", "sut_outbound", "generator_local", "network", "all"] = "all"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class MessageInput(ToolInput):
    message: str


class StepInput(OptionalRequestInput):
    step: str


class AnnotationInput(RequestInput):
    step: str
    annotations: list[str] | None = None
    constraints: list[str] | None = None


class FieldRelation(ToolInput):
    prior_field: str
    dependent_field: str
    derive: str | None = None


class RemoveDependencyInput(RequestInput):
    prior_step: str
    dependent_step: str


class UpsertDependencyInput(RemoveDependencyInput):
    condition: str | None = None
    field_relations: list[FieldRelation] | None = None


class TargetInput(ToolInput):
    handler_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FunctionSourceInput(OptionalRequestInput):
    function: str | None = None
    functions: list[str] | None = Field(default=None, min_length=1, max_length=8)
    whole: bool = False
    max_lines: int | None = Field(default=None, ge=10, le=2000)
    around_line: int | None = Field(default=None, ge=1)
    radius: int = Field(default=120, ge=10, le=300)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReplayLogsInput(RequestInput):
    source: Literal["stdout", "stderr", "both"] = "both"
    case_id: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    max_lines: int = Field(default=200, ge=10, le=500)


class MutableField(ToolInput):
    field: str = Field(min_length=1)
    kind: Literal["string", "bytes", "integer", "boolean", "list", "map"] | None = None
    encoding: Literal["hex", "base64"] | None = None
    default: Any = None
    suggestion: str | None = Field(default=None, min_length=1)
    evidence: str | None = Field(default=None, min_length=1)
    source: str | None = None


class TraceStep(ToolInput):
    name: str = Field(description="Unique step instance name within this trace.")
    step: str = Field(description="Exact protocol model step name.")
    channel: str
    reason: str
    sender: str | None = None
    receiver: str | None = None
    party: str | None = None
    constraints: list[str] | None = None
    mutable_fields: list[MutableField] | None = None


class ProposalGoal(ToolInput):
    handler_ids: list[str] = Field(default_factory=list)
    reason: str


class ProposalChannel(ToolInput):
    channel: str
    endpoints: list[str]


class ProposalDependency(ToolInput):
    prior: str
    dependent: str
    kind: Literal["order", "data", "resource", "state"] | None = None
    condition: str | None = None
    fields: list[Annotated[str, Field(min_length=1)]] | None = Field(
        default=None, description="State keys saved by the prior step and read by the dependent step."
    )


class SubmitProposalInput(RequestInput):
    origin: Literal["coverage", "autonomous"] = "coverage"
    goal: ProposalGoal | None = None
    parties: dict[str, str]
    channels: dict[str, ProposalChannel]
    trace: list[TraceStep]
    dependencies: list[ProposalDependency] | None = None

# Common output structures are shared between tools, including nested pages.
T = TypeVar("T")


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Page(OutputModel, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class BranchStats(OutputModel):
    total_branches: int
    covered_branches: int
    missed_branches: int
    branch_coverage_percent: float | None


class Handler(OutputModel):
    id: str
    symbol: str
    location: str
    evidence: str
    uncertain: bool


class SourceLine(OutputModel):
    line: int
    text: str


class Branch(OutputModel):
    file: str
    line: int
    column: int
    end_line: int
    end_column: int
    true_count: int
    false_count: int


class LineRange(OutputModel):
    start: int
    end: int


class FunctionRange(LineRange):
    basis: Literal["tree_sitter"]
    first_branch_line: int | None = None
    last_branch_line: int | None = None


class Remaining(OutputModel):
    before: LineRange | None
    after: LineRange | None


class SourceWindow(OutputModel):
    path: str
    start: int
    end: int
    lines: list[SourceLine]
    branches: list[Branch]
    total_lines: int
    remaining: Remaining
    function: str | None = None
    file: str | None = None
    stats: BranchStats | None = None
    range: FunctionRange | None = None


class SourceBundleEntry(OutputModel):
    function: str
    file: str
    range: FunctionRange
    stats: BranchStats | None = None
    lines: list[SourceLine] | None = None
    branches: list[Branch] | None = None
    truncated: bool | None = None
    remaining_after: LineRange | None = None
    skipped: bool | None = None
    reason: str | None = None


class SourceBundle(OutputModel):
    functions: list[SourceBundleEntry]
    max_lines: int
    lines_returned: int
    files: list[str]


class Target(OutputModel):
    handler_id: str
    reason: str


class TargetStatus(OutputModel):
    request_id: str
    target: Target | None


class BeginAnalysisOutput(OutputModel):
    request_id: str
    graph_revision: int
    steps: int


class SubmitProposalOutput(OutputModel):
    request_id: str
    graph_revision: int
    proposal_sha256: str
    path: str


class DependencyEditOutput(OutputModel):
    prior: str
    dependent: str
    graph_revision: int


class AnnotationOutput(OutputModel):
    step: "ModelStep"
    graph_revision: int


class FieldRelationOutput(OutputModel):
    prior_field: str
    dependent_field: str
    derive: str | None = None


class DependencyNeighbor(OutputModel):
    step: str
    condition: str
    field_relations: list[FieldRelationOutput]


class StepDependenciesOutput(OutputModel):
    step: str
    before: list[DependencyNeighbor]
    after: list[DependencyNeighbor]
    graph_revision: int


class ReachableFunction(OutputModel):
    symbol: str
    distance: int


class ReachableOutput(OutputModel):
    function: str
    functions: list[ReachableFunction]
    total: int
    truncated: bool


class ReachingHandler(OutputModel):
    handler: str
    path: list[str]
    handler_ids: list[str]


class ReachingOutput(OutputModel):
    function: str
    handlers: list[ReachingHandler]


class LogLine(OutputModel):
    source: str
    line: int
    text: str


class LogCase(OutputModel):
    case_id: str
    start: int
    end: int
    outcome: str | None = None
    source: str


class ReplayLogsOutput(OutputModel):
    snapshot: str
    sources: list[str]
    lines: list[LogLine]
    truncated: bool
    cases: list[LogCase]
    case_count: int

class ModelStep(OutputModel):
    name: str
    action: str
    message: str | None
    channel: str
    sender: str | None
    receiver: str | None
    party: str | None
    condition: str
    annotations: list[str]
    constraints: list[str]


class StepReference(OutputModel):
    step: str
    condition: str


class HandlerStep(ModelStep):
    predecessors: list[StepReference]
    predecessors_total: int


class HandlerMapping(OutputModel):
    message: str
    handler: Handler
    steps: list[HandlerStep]


class HandlerCoverage(OutputModel):
    own: BranchStats | None
    cumulative: BranchStats | None
    reachable_functions: int
    status: str


class HandlerCoverageRow(HandlerCoverage):
    message: str
    handler: Handler


class HandlerCoverageOutput(OutputModel):
    handlers: Page[HandlerCoverageRow]
    total: int


class SourceError(OutputModel):
    code: str
    message: str


class DescribeHandlerOutput(OutputModel):
    request_id: str
    handler: Handler
    mappings: Page[HandlerMapping]
    coverage: HandlerCoverage
    source: SourceWindow | None
    source_error: SourceError | None
    graph_revision: int


class ProcessingOperation(OutputModel):
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    rule: str


class Processing(OutputModel):
    before_send: list[ProcessingOperation] = Field(default_factory=list)
    after_send: list[ProcessingOperation] = Field(default_factory=list)
    on_receive: list[ProcessingOperation] = Field(default_factory=list)


class TraceStepView(ModelStep):
    processing: Processing | None
    other_predecessors: list[str]
    other_predecessors_total: int


class GraphDependency(OutputModel):
    prior: str
    dependent: str
    condition: str
    field_relations: list[FieldRelationOutput]


class DescribeTraceOutput(OutputModel):
    request_id: str
    graph_revision: int
    steps: Page[TraceStepView]
    messages: dict[str, list[str]]
    dependencies: Page[GraphDependency]


class ProtocolStepsOutput(OutputModel):
    protocol_name: str
    binding: dict[str, str]
    steps: Page[ModelStep]
    total: int


class MessageCarrier(OutputModel):
    step: str
    action: str
    channel: str
    sender: str | None
    receiver: str | None
    party: str | None
    direction: str


class MessageRow(OutputModel):
    message: str
    direction: str
    fields: list[str]
    steps: list[str]


class ListMessagesOutput(OutputModel):
    total: int
    offset: int
    limit: int
    items: list[MessageRow]


class ShowMessageOutput(OutputModel):
    message: str
    fields: list[str]
    steps: list[MessageCarrier]


class StepNeed(OutputModel):
    step: str
    action: str
    condition: str


class ShowStepOutput(OutputModel):
    step: str
    action: str
    message: str | None
    channel: str | None
    sender: str | None
    receiver: str | None
    party: str | None
    condition: str
    processing: Processing | None
    total_predecessors: int
    needs: list[StepNeed]


class ModuleRow(BranchStats):
    module: str


class FileRow(BranchStats):
    file: str


class FunctionStatsRow(BranchStats):
    function: str


class OverviewOutput(BranchStats):
    modules: list[ModuleRow]
    total: int
    next_offset: int | None
    source_root: str
    files: int
    functions: int


class ModuleCoverageOutput(BranchStats):
    path: str
    children: Page[ModuleRow | FileRow]


class SingleFileCoverageOutput(BranchStats):
    file: str
    functions: list[FunctionStatsRow]


class AllFileCoverageOutput(BranchStats):
    files: Page[FileRow]


class FunctionRow(OutputModel):
    symbol: str
    files: list[str]
    own: BranchStats | None
    cumulative: BranchStats | None
    status: str
    reachable_functions: int | None = None


class AllFunctionsOutput(OutputModel):
    functions: Page[FunctionRow]
    total: int

class LoadedDataset(OutputModel):
    loaded: bool
    version: int | None = None
    digest: str | None = None
    build: str | None = None
    snapshot: str | None = None
    binary: str | None = None
    coverage_path: str | None = None
    cfg_dir: str | None = None
    functions: int | None = None
    callgraph: str | None = None
    protocol_name: str | None = None
    steps: int | None = None
    revision: int | None = None
    handlers_path: str | None = None
    messages: int | None = None
    handlers: int | None = None


class LoadedStateOutput(OutputModel):
    coverage: LoadedDataset
    cfg: LoadedDataset
    callgraph: LoadedDataset
    protocol: LoadedDataset
    handlers: LoadedDataset
    graph_revision: int
    committed_proposal: str | None


class LoadCoverageOutput(OutputModel):
    version: int
    build: str
    snapshot: str
    binary: str | None
    coverage_path: str
    coverage: str | None = None
    reused: bool | None = None


class LoadCfgOutput(OutputModel):
    version: int
    cfg_dir: str
    functions: int


class LoadCallgraphOutput(OutputModel):
    version: int
    callgraph: str
    functions: int
    aliases: int


class LoadProtocolOutput(OutputModel):
    version: int
    protocol_name: str
    steps: int
    revision: int


class WriteHandlersOutput(OutputModel):
    handlers_path: str
    messages: int
    handlers: int
    bytes: int
    version: int | None = None
    digest: str | None = None


class LoadHandlersOutput(OutputModel):
    version: int
    handlers_path: str
    messages: int
    handlers: int


# Each tool has exactly one input and output contract. Union outputs describe
# documented variants without changing their wire shape.
TOOL_MODELS = {
    "get_loaded_state": (EmptyInput, LoadedStateOutput),
    "load_coverage_json": (CoverageInput, LoadCoverageOutput),
    "load_cfg": (CfgInput, LoadCfgOutput),
    "load_callgraph": (CallgraphInput, LoadCallgraphOutput),
    "load_protocol": (ProtocolInput, LoadProtocolOutput),
    "load_handlers": (HandlersInput, LoadHandlersOutput),
    "write_handlers": (WriteHandlersInput, WriteHandlersOutput),
    "show_overview_code_coverage": (PageInput, OverviewOutput),
    "show_module_code_coverage": (ModuleInput, ModuleCoverageOutput),
    "show_file_code_coverage": (FileInput, SingleFileCoverageOutput | AllFileCoverageOutput),
    "show_handler_code_coverage": (HandlerCoverageInput, HandlerCoverageOutput),
    "show_func_code_coverage": (FunctionCoverageInput, FunctionRow | AllFunctionsOutput),
    "get_reachable_functions": (ReachableInput, ReachableOutput),
    "get_reaching_handlers": (ReachingInput, ReachingOutput),
    "describe_handler": (DescribeHandlerInput, DescribeHandlerOutput),
    "describe_trace_steps": (DescribeTraceInput, DescribeTraceOutput),
    "show_source_coverage": (SourceCoverageInput, SourceWindow),
    "get_protocol_steps": (ProtocolStepsInput, ProtocolStepsOutput),
    "list_parties": (ListPartiesInput, ListPartiesOutput),
    "get_step_dependencies": (StepDependenciesInput, StepDependenciesOutput),
    "list_messages": (ListMessagesInput, ListMessagesOutput),
    "show_message": (MessageInput, ShowMessageOutput),
    "show_step": (StepInput, ShowStepOutput),
    "update_step_annotations": (AnnotationInput, AnnotationOutput),
    "upsert_dependency": (UpsertDependencyInput, DependencyEditOutput),
    "remove_dependency": (RemoveDependencyInput, DependencyEditOutput),
    "begin_analysis": (RequestInput, BeginAnalysisOutput),
    "select_analysis_target": (TargetInput, TargetStatus),
    "get_analysis_target": (EmptyInput, TargetStatus),
    "switch_analysis_target": (TargetInput, TargetStatus),
    "show_function_source": (FunctionSourceInput, SourceWindow | SourceBundle),
    "show_replay_logs": (ReplayLogsInput, ReplayLogsOutput),
    "submit_proposal": (SubmitProposalInput, SubmitProposalOutput),
}
