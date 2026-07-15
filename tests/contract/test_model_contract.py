"""Static contract baseline before behavior is extracted."""

from scs.models import PROTOCOL_VERSION, ProtocolRange, RepositoryIndexState
from scs.wire.models import ErrorCode, WireError, WireErrorResponse


def test_protocol_ranges_overlap_only_when_compatible() -> None:
    assert ProtocolRange(minimum=1, maximum=2).overlaps(
        ProtocolRange(minimum=2, maximum=3)
    )
    assert not ProtocolRange(minimum=1, maximum=1).overlaps(
        ProtocolRange(minimum=2, maximum=2)
    )


def test_unknown_additive_fields_are_ignored() -> None:
    parsed = ProtocolRange.model_validate({"minimum": 1, "maximum": 1, "future": True})
    assert parsed == ProtocolRange(minimum=1, maximum=1)


def test_wire_errors_are_machine_readable() -> None:
    response = WireErrorResponse(
        id="request-1",
        error=WireError(
            code=ErrorCode.INCOMPATIBLE_PROTOCOL,
            message="No overlapping SCSWire version",
        ),
    )
    assert response.version == PROTOCOL_VERSION
    assert response.error.code is ErrorCode.INCOMPATIBLE_PROTOCOL


def test_repository_states_do_not_conflate_unindexed_and_failed() -> None:
    assert RepositoryIndexState.UNINDEXED != RepositoryIndexState.FAILED

