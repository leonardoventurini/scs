"""Narrow typing for the ONNX Runtime APIs used by the optional Laya worker."""

from collections.abc import Mapping, Sequence

__version__: str


class SessionOptions:
    intra_op_num_threads: int


class InferenceSession:
    def __init__(
        self,
        path_or_bytes: str,
        sess_options: SessionOptions | None = None,
        providers: Sequence[str] | None = None,
    ) -> None: ...

    def run(
        self,
        output_names: Sequence[str],
        input_feed: Mapping[str, object],
    ) -> list[object]: ...
