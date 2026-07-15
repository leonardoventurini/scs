//! FFI bindings for the SCS knowledge graph engine.
//!
//! Provides Python (PyO3) bindings via a native Python module
//! named `_scs_native`, with `PyKnowledgeGraph` and parser functions.

#[cfg(feature = "python")]
mod python;
