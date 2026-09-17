"""Concurrency contracts for graph structural mutations and bounded reads."""

from __future__ import annotations

import threading

import pytest

from scs.graph.gating import GraphBusyError, GraphMutationGate


def test_read_fails_immediately_while_mutation_is_reserved() -> None:
    gate = GraphMutationGate()
    mutation_entered = threading.Event()
    release_mutation = threading.Event()

    def mutation() -> None:
        with gate.mutation():
            mutation_entered.set()
            release_mutation.wait(timeout=2)

    worker = threading.Thread(target=mutation)
    worker.start()
    assert mutation_entered.wait(timeout=2)

    with pytest.raises(GraphBusyError, match="temporarily busy"):
        with gate.read():
            pass

    release_mutation.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_mutation_waits_for_existing_reader_then_blocks_new_readers() -> None:
    gate = GraphMutationGate()
    read_entered = threading.Event()
    release_read = threading.Event()
    mutation_entered = threading.Event()

    def reader() -> None:
        with gate.read():
            read_entered.set()
            release_read.wait(timeout=2)

    def mutation() -> None:
        with gate.mutation():
            mutation_entered.set()

    reader_worker = threading.Thread(target=reader)
    mutation_worker = threading.Thread(target=mutation)
    reader_worker.start()
    assert read_entered.wait(timeout=2)
    mutation_worker.start()
    assert not mutation_entered.wait(timeout=0.05)

    release_read.set()
    reader_worker.join(timeout=2)
    mutation_worker.join(timeout=2)
    assert not reader_worker.is_alive()
    assert not mutation_worker.is_alive()
    assert mutation_entered.is_set()
