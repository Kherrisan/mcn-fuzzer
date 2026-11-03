"""Python-owned IDs, name references, and atomic private-state commits."""

import asyncio
from copy import deepcopy

import pytest
from protocol_samples import base, document, interaction_response, interactions, object_response
from pydantic_ai.messages import ModelRequest, UserPromptPart

from rsc.protocol.agent_runtime import EventWriter, encode
from rsc.protocol.context import Corpus
from rsc.protocol.contract import BASE
from rsc.protocol.ids import GlobalIdAllocator, IdConsistencyError
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.models import InteractionExtractionResponse, ObjectExtractionResponse
from rsc.protocol.proposals import (
    CandidateState,
    ProposalError,
    ProposalValidator,
    commit_proposal,
)
from rsc.protocol.retrieval import RetrievalContext
from rsc.protocol.tools import AgentDeps
from rsc.protocol.validate import object_errors


def environment(state=None, *, phase="objects", read_inventory=False):
    state = state or CandidateState()
    doc = document()
    corpus = Corpus({doc["meta"]["source_id"]: doc})
    context = RetrievalContext(corpus)
    for key in corpus.blocks:
        context.add(*key, full=True)
    deps = AgentDeps(
        "test",
        phase,
        phase,
        corpus,
        state.registry(corpus.documents),
        KeywordIndex("test", corpus),
        context,
        EventWriter(),
    )
    deps.before_request(
        [
            ModelRequest(
                [
                    UserPromptPart(
                        encode(deps.payload(inventory=state.model if read_inventory else {}))
                    )
                ]
            )
        ],
        {"run_id": "test", "request_index": 1},
    )
    return state, deps, ProposalValidator(state, deps)


def typed_objects(data):
    return ObjectExtractionResponse.model_validate(data)


def test_python_assigns_only_step_ids_and_keeps_issue_names():
    async def run():
        state, _, validator = environment()
        data = object_response(base())
        data["unresolved"] = [
            {"description": "Role scope needs checking", "object_names": ["requester"]}
        ]
        allocator = GlobalIdAllocator("test")
        result = await commit_proposal(
            validator, typed_objects(data), allocator, origin="doc:block", list_id="list-1"
        )
        assert state.revision == 1
        assert allocator.reservations == {}
        assert all(
            "id" not in row
            for kind in BASE
            for row in state.model[kind]
        )
        assert state.issues[0] == {
            "description": "Role scope needs checking",
            "object_names": ["requester"],
        }
        assert result["reused_names"] == []
        assert state.model["channels"][0]["endpoint_party_names"] == ["requester", "responder"]
        assert object_errors(state.model, validator.deps.corpus.documents) == {}

    asyncio.run(run())


def test_exact_existing_name_reuses_python_identity_without_objectreuse():
    async def run():
        state, _, first = environment()
        allocator = GlobalIdAllocator("test")
        proposal = object_response({"parties": [base()["parties"][0]]})
        await commit_proposal(first, typed_objects(proposal), allocator, origin="one", list_id="a")
        assert "id" not in state.model["parties"][0]
        _, _, second = environment(state, read_inventory=True)
        result = await commit_proposal(
            second, typed_objects(proposal), allocator, origin="two", list_id="a"
        )
        assert result["items"]["parties"][0]["name"] == "requester"
        assert len(state.model["parties"]) == 1
        assert allocator.reservations == {}
        assert "reused_objects" not in proposal

    asyncio.run(run())


def test_missing_name_reference_is_kept_and_does_not_start_completion():
    async def run():
        state, _, objects_validator = environment()
        allocator = GlobalIdAllocator("test")
        await commit_proposal(
            objects_validator,
            typed_objects(object_response(base())),
            allocator,
            origin="objects",
            list_id="a",
        )
        _, _, validator = environment(state, phase="interactions", read_inventory=True)
        proposal = interaction_response(interactions(state.model))
        proposal["steps"][0]["message_name"] = "not-a-message"
        proposal["steps"][0]["sender_name"] = "ghost"
        proposal["step_dependencies"][0]["field_relations"][0]["prior_field"] = "not-a-field"
        await commit_proposal(
            validator,
            InteractionExtractionResponse.model_validate(proposal),
            allocator,
            origin="interactions",
            list_id="a",
        )
        assert state.model["steps"][0]["message_name"] == "not-a-message"
        assert state.model["steps"][0]["sender_name"] == "ghost"
        assert object_errors(state.model, validator.deps.corpus.documents) == {}
        assert state.issues == []

    asyncio.run(run())


def test_interaction_definition_ids_are_injected_and_local_names_are_resolved():
    async def run():
        state, _, objects_validator = environment()
        allocator = GlobalIdAllocator("test")
        await commit_proposal(
            objects_validator,
            typed_objects(object_response(base())),
            allocator,
            origin="objects",
            list_id="a",
        )
        _, _, validator = environment(state, phase="interactions", read_inventory=True)
        proposal = interaction_response(interactions(state.model))
        typed = InteractionExtractionResponse.model_validate(proposal)
        await commit_proposal(validator, typed, allocator, origin="interactions", list_id="a")
        assert len(state.model["steps"]) == 2
        assert len(state.model["step_dependencies"]) == 4
        assert all(not row["id"].startswith("local:") for row in state.model["steps"])
        assert object_errors(state.model, validator.deps.corpus.documents) == {}

    asyncio.run(run())


def test_new_facts_do_not_require_search_but_edits_require_the_definition():
    async def run():
        state, _, first = environment()
        allocator = GlobalIdAllocator("test")
        await commit_proposal(
            first,
            typed_objects(object_response({"parties": [base()["parties"][0]]})),
            allocator,
            origin="one",
            list_id="a",
        )
        _, _, second = environment(state)
        created = object_response(
            {
                "parties": [
                    {"name": "peer", "description": "another participant"},
                ]
            }
        )
        await commit_proposal(second, typed_objects(created), allocator, origin="two", list_id="a")
        assert {row["name"] for row in state.model["parties"]} == {"requester", "peer"}
        _, _, editor = environment(state)
        edited = object_response(
            {"parties": [{"name": "requester", "description": "changed role"}]}
        )
        with pytest.raises(ProposalError, match="complete definition"):
            await commit_proposal(
                editor, typed_objects(edited), allocator, origin="three", list_id="a"
            )

    asyncio.run(run())


@pytest.mark.parametrize("remaining_fields", [[], [{"name": "CSeq"}]])
def test_repair_can_remove_message_fields_only_after_scoped_full_read(remaining_fields):
    async def run():
        state = CandidateState()
        state.model["messages"] = [
            {
                "name": "request",
                "associated_fields": [{"name": "CSeq"}, {"name": "unrelated payload"}],
            }
        ]
        original = state.snapshot()
        replacement = typed_objects(
            object_response(
                {"messages": [{"name": "request", "associated_fields": remaining_fields}]}
            )
        )
        allocator = GlobalIdAllocator("test")
        _, unread_deps, _ = environment(state)
        unread = ProposalValidator(state, unread_deps, repair=True)
        with pytest.raises(ProposalError, match="complete definition"):
            await commit_proposal(
                unread, replacement, allocator, origin="unread", list_id=None
            )
        assert state.model == original
        _, read_deps, _ = environment(state, read_inventory=True)
        out_of_scope = ProposalValidator(state, read_deps, repair=True, editable_ids=set())
        with pytest.raises(ProposalError, match="edit scope"):
            await commit_proposal(
                out_of_scope, replacement, allocator, origin="outside", list_id=None
            )
        assert state.model == original
        authorized = ProposalValidator(
            state, read_deps, repair=True, editable_ids=set(read_deps.registry.rows)
        )
        await commit_proposal(
            authorized, replacement, allocator, origin="correction", list_id=None
        )
        assert state.model["messages"] == [
            {"name": "request", "associated_fields": remaining_fields}
        ]
        assert state.revision == 1
        assert not allocator.reservations

    asyncio.run(run())


@pytest.mark.parametrize("remaining_count", [0, 1])
def test_channel_source_cleanup_requires_repair_scope_and_full_read(remaining_count):
    async def run():
        state = CandidateState()
        channel = deepcopy(base()["channels"][0])
        channel["endpoint_sources"] = [
            {"party_name": "responder", "parameter": "port", "from": "21"},
            {"party_name": "requester", "parameter": "port", "from": "incorrect source"},
        ]
        state.model["channels"] = [channel]
        original = state.snapshot()
        corrected = deepcopy(channel)
        corrected["endpoint_sources"] = corrected["endpoint_sources"][:remaining_count]
        replacement = typed_objects(object_response({"channels": [corrected]}))
        allocator = GlobalIdAllocator("test")

        _, unread_deps, _ = environment(state)
        unread = ProposalValidator(state, unread_deps, repair=True)
        with pytest.raises(ProposalError, match="complete definition"):
            await commit_proposal(unread, replacement, allocator, origin="unread", list_id=None)
        assert state.model == original

        _, read_deps, extraction = environment(state, read_inventory=True)
        with pytest.raises(
            ProposalError, match="cannot delete or replace existing extraction entries"
        ):
            await commit_proposal(
                extraction, replacement, allocator, origin="extraction", list_id="a"
            )
        assert state.model == original

        outside = ProposalValidator(state, read_deps, repair=True, editable_ids=set())
        with pytest.raises(ProposalError, match="edit scope"):
            await commit_proposal(outside, replacement, allocator, origin="outside", list_id=None)
        assert state.model == original

        authorized = ProposalValidator(
            state, read_deps, repair=True, editable_ids=set(read_deps.registry.rows)
        )
        await commit_proposal(
            authorized, replacement, allocator, origin="source correction", list_id=None
        )
        assert state.model["channels"] == [corrected]
        assert state.revision == 1 and not allocator.reservations

    asyncio.run(run())


def test_channel_correction_does_not_guess_between_read_same_name_targets():
    async def run():
        state = CandidateState()
        first = deepcopy(base()["channels"][0])
        first["endpoint_sources"] = [
            {"party_name": "requester", "parameter": "port", "from": "first source"}
        ]
        second = deepcopy(first)
        second["endpoint_sources"][0]["from"] = "second source"
        state.model["channels"] = [first, second]
        original = state.snapshot()
        corrected = deepcopy(first)
        corrected["endpoint_sources"] = []
        corrected["initiator_party_name"] = "responder"
        _, deps, _ = environment(state, read_inventory=True)
        validator = ProposalValidator(
            state, deps, repair=True, editable_ids=set(deps.registry.rows)
        )
        with pytest.raises(ProposalError, match="multiple editable definitions"):
            await commit_proposal(
                validator,
                typed_objects(object_response({"channels": [corrected]})),
                GlobalIdAllocator("test"),
                origin="ambiguous correction",
                list_id=None,
            )
        assert state.model == original and state.revision == 0

    asyncio.run(run())


def test_concurrent_private_commits_share_only_atomic_allocator():
    async def run():
        allocator = GlobalIdAllocator("test")

        async def commit(number):
            state, _, validator = environment()
            data = object_response({"parties": [base()["parties"][0]]})
            await commit_proposal(
                validator,
                typed_objects(data),
                allocator,
                origin="doc:same",
                list_id=f"list-{number}",
            )
            return state

        states = await asyncio.gather(*(commit(number) for number in range(5)))
        assert len({state.model["parties"][0]["name"] for state in states}) == 1
        assert all("id" not in state.model["parties"][0] for state in states)
        state, _, validator = environment(phase="interactions")
        broken = GlobalIdAllocator("test", id_factory=lambda _: "collision")
        steps = interaction_response(interactions(base()))
        with pytest.raises(IdConsistencyError, match="collision"):
            await commit_proposal(
                validator,
                InteractionExtractionResponse.model_validate(steps),
                broken,
                origin="doc:two",
                list_id="a",
            )
        assert state.revision == 0 and all(not rows for rows in state.model.values())
        assert not broken.reservations

    asyncio.run(run())
