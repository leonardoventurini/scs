//! Language-specific AST parsers for code ingestion.
//!
//! All parsers produce the same output types ([`ParsedEntity`], [`ParsedEdge`])
//! regardless of the source language. This allows the ingestion pipeline
//! to treat all languages uniformly.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use tree_sitter::Node;

use scs_core::node_types::NodeType;

pub mod bash;
pub mod css;
pub mod elixir;
pub mod python;
pub mod registry;
pub mod rust;
pub mod swift;
pub mod typescript;

/// Max bytes for raw_text on major entities (classes, functions, methods).
pub const RAW_TEXT_LIMIT: usize = 2048;
/// Max bytes for raw_text on minor entities (variables, constants, signatures).
pub const RAW_TEXT_SMALL_LIMIT: usize = 512;
/// Max bytes for docstrings in embed_text output.
pub const DOCSTRING_LIMIT: usize = 1024;

/// Truncate a string to at most `max_bytes` without splitting a multi-byte
/// UTF-8 character. Always returns a valid `&str`.
pub fn truncate_str(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    // Walk backwards from the limit to find a char boundary.
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Count McCabe cyclomatic complexity for a function/method body.
///
/// Walks all descendants of the given node, counting language-specific
/// decision-point nodes. The baseline is 1 (a straight-line function),
/// plus one for each branch, loop, exception handler, or short-circuit
/// boolean operator.
pub fn count_complexity(node: &Node, source: &[u8], language: &str) -> u32 {
    let decision_nodes: HashSet<&str> = match language {
        "python" => [
            "if_statement",
            "elif_clause",
            "for_statement",
            "while_statement",
            "except_clause",
            "with_statement",
            "assert_statement",
            "conditional_expression",
            "list_comprehension",
            "set_comprehension",
            "dictionary_comprehension",
            "generator_expression",
        ]
        .into_iter()
        .collect(),

        "typescript" | "javascript" | "tsx" | "jsx" => [
            "if_statement",
            "for_statement",
            "for_in_statement",
            "while_statement",
            "do_statement",
            "catch_clause",
            "ternary_expression",
        ]
        .into_iter()
        .collect(),

        "rust" => [
            "if_expression",
            "if_let_expression",
            "for_expression",
            "while_expression",
            "while_let_expression",
        ]
        .into_iter()
        .collect(),

        "swift" => [
            "if_statement",
            "guard_statement",
            "for_statement",
            "while_statement",
            "repeat_while_statement",
            "catch_clause",
            "ternary_expression",
        ]
        .into_iter()
        .collect(),

        "bash" => [
            "if_statement",
            "elif_clause",
            "for_statement",
            "while_statement",
            "c_style_for_statement",
            "case_item",
        ]
        .into_iter()
        .collect(),

        "elixir" => HashSet::new(), // Elixir uses call-based dispatch below.

        _ => HashSet::new(),
    };

    let mut count: u32 = 0;
    let mut cursor = node.walk();
    // Walk all descendants via tree-sitter cursor.
    let mut reached_root = false;
    loop {
        let n = cursor.node();

        if decision_nodes.contains(n.kind()) {
            count += 1;
        }

        // Language-specific compound checks.
        match language {
            "python"
                // `and` / `or` boolean operators.
                if n.kind() == "boolean_operator" => {
                    count += 1;
                }
            "typescript" | "javascript" | "tsx" | "jsx" => {
                // `switch_case` but not default.
                if n.kind() == "switch_case" {
                    // A switch_case with a value (not `default:`) is a decision.
                    let text =
                        std::str::from_utf8(&source[n.start_byte()..n.end_byte()]).unwrap_or("");
                    if !text.starts_with("default") {
                        count += 1;
                    }
                }
                // `&&`, `||`, `??` in binary expressions.
                if n.kind() == "binary_expression" {
                    let mut child_cursor = n.walk();
                    for child in n.children(&mut child_cursor) {
                        let text =
                            std::str::from_utf8(&source[child.start_byte()..child.end_byte()])
                                .unwrap_or("");
                        if text == "&&" || text == "||" || text == "??" {
                            count += 1;
                        }
                    }
                }
            }
            "rust" => {
                // match_arm (non-wildcard).
                if n.kind() == "match_arm" {
                    count += 1;
                }
                // `&&` / `||` in binary expressions.
                if n.kind() == "binary_expression" {
                    let mut child_cursor = n.walk();
                    for child in n.children(&mut child_cursor) {
                        let text =
                            std::str::from_utf8(&source[child.start_byte()..child.end_byte()])
                                .unwrap_or("");
                        if text == "&&" || text == "||" {
                            count += 1;
                        }
                    }
                }
            }
            "swift"
                // switch_case.
                if n.kind() == "switch_case" => {
                    count += 1;
                }
            "bash"
                // `&&` / `||` operators in pipelines or list constructs.
                if (n.kind() == "pipeline" || n.kind() == "list") => {
                    let text =
                        std::str::from_utf8(&source[n.start_byte()..n.end_byte()]).unwrap_or("");
                    // Count && and || operators (they join commands).
                    count += text.matches("&&").count() as u32;
                    count += text.matches("||").count() as u32;
                }
            "elixir" => {
                // Control flow is via `call` nodes: if/unless/cond/case/with/try.
                if n.kind() == "call" {
                    let mut child_cursor = n.walk();
                    for child in n.children(&mut child_cursor) {
                        if child.kind() == "identifier" {
                            let text =
                                std::str::from_utf8(&source[child.start_byte()..child.end_byte()])
                                    .unwrap_or("");
                            if matches!(text, "if" | "unless" | "cond" | "case" | "with" | "try") {
                                count += 1;
                            }
                            break;
                        }
                    }
                }
                // fn clause arms.
                if n.kind() == "stab_clause" {
                    count += 1;
                }
                // `and`/`or`/`&&`/`||` binary operators.
                if n.kind() == "binary_operator" {
                    let mut child_cursor = n.walk();
                    for child in n.children(&mut child_cursor) {
                        let text =
                            std::str::from_utf8(&source[child.start_byte()..child.end_byte()])
                                .unwrap_or("");
                        if matches!(text, "and" | "or" | "&&" | "||") {
                            count += 1;
                        }
                    }
                }
            }
            _ => {}
        }

        // Depth-first traversal via tree-sitter cursor.
        if cursor.goto_first_child() {
            continue;
        }
        if cursor.goto_next_sibling() {
            continue;
        }
        loop {
            if !cursor.goto_parent() {
                reached_root = true;
                break;
            }
            if cursor.goto_next_sibling() {
                break;
            }
        }
        if reached_root {
            break;
        }
    }

    1 + count
}

/// Extract the simple callee name from a call expression node.
///
/// Navigates language-specific child structures to find the rightmost
/// identifier — the actual function/method name being called.
/// Returns `None` for patterns we can't resolve (e.g., computed calls).
fn extract_callee_name(node: &Node, source: &[u8], language: &str) -> Option<String> {
    let text_of = |n: &Node| -> &str {
        std::str::from_utf8(&source[n.start_byte()..n.end_byte()]).unwrap_or("")
    };

    match language {
        "python" => {
            // Python `call` node: child named "function" is the callee.
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                match child.kind() {
                    "identifier" if child == node.child(0)? => {
                        return Some(text_of(&child).to_string());
                    }
                    "attribute" => {
                        // obj.method() — take the rightmost identifier.
                        let mut attr_cursor = child.walk();
                        let mut last_ident = None;
                        for sub in child.children(&mut attr_cursor) {
                            if sub.kind() == "identifier" {
                                last_ident = Some(text_of(&sub).to_string());
                            }
                        }
                        return last_ident;
                    }
                    _ => {}
                }
            }
            None
        }
        "typescript" | "javascript" | "tsx" | "jsx" => {
            // TS/JS `call_expression`: first child is the callee.
            let callee = node.child(0)?;
            match callee.kind() {
                "identifier" => Some(text_of(&callee).to_string()),
                "member_expression" => {
                    // obj.method() — find the property identifier.
                    let mut cursor = callee.walk();
                    for child in callee.children(&mut cursor) {
                        if child.kind() == "property_identifier" {
                            return Some(text_of(&child).to_string());
                        }
                    }
                    None
                }
                "new_expression" => {
                    // new Foo() — extract the constructor name.
                    let mut cursor = callee.walk();
                    for child in callee.children(&mut cursor) {
                        if child.kind() == "identifier" {
                            return Some(text_of(&child).to_string());
                        }
                    }
                    None
                }
                _ => None,
            }
        }
        "rust" => {
            // Rust `call_expression`: first child is the callee.
            let callee = node.child(0)?;
            match callee.kind() {
                "identifier" => Some(text_of(&callee).to_string()),
                "field_expression" => {
                    // obj.method() — find field_identifier.
                    let mut cursor = callee.walk();
                    for child in callee.children(&mut cursor) {
                        if child.kind() == "field_identifier" {
                            return Some(text_of(&child).to_string());
                        }
                    }
                    None
                }
                "scoped_identifier" => {
                    // Type::method() — take the last identifier segment.
                    let mut cursor = callee.walk();
                    let mut last_ident = None;
                    for child in callee.children(&mut cursor) {
                        if child.kind() == "identifier" {
                            last_ident = Some(text_of(&child).to_string());
                        }
                    }
                    last_ident
                }
                _ => None,
            }
        }
        "swift" => {
            // Swift `call_expression`: first child is the callee.
            let callee = node.child(0)?;
            match callee.kind() {
                "simple_identifier" => Some(text_of(&callee).to_string()),
                "navigation_expression" => {
                    // obj.method() — extract the method name from navigation_suffix.
                    // AST: navigation_expression -> [self_expression, navigation_suffix -> [., simple_identifier]]
                    let mut cursor = callee.walk();
                    let mut last_ident = None;
                    for child in callee.children(&mut cursor) {
                        if child.kind() == "simple_identifier" {
                            last_ident = Some(text_of(&child).to_string());
                        } else if child.kind() == "navigation_suffix" {
                            // The simple_identifier lives inside navigation_suffix.
                            let mut sub_cursor = child.walk();
                            for sub in child.children(&mut sub_cursor) {
                                if sub.kind() == "simple_identifier" {
                                    last_ident = Some(text_of(&sub).to_string());
                                }
                            }
                        }
                    }
                    last_ident
                }
                _ => None,
            }
        }
        "elixir" => {
            // Elixir `call` node: first child is identifier or dot.
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.kind() == "identifier" {
                    return Some(text_of(&child).to_string());
                }
                if child.kind() == "dot" {
                    // Module.function() — extract rightmost identifier.
                    let mut dot_cursor = child.walk();
                    let mut last_ident = None;
                    for sub in child.children(&mut dot_cursor) {
                        if sub.kind() == "identifier" {
                            last_ident = Some(text_of(&sub).to_string());
                        }
                    }
                    return last_ident;
                }
            }
            None
        }
        _ => None,
    }
}

/// Extract unique callee names from a function/method body.
///
/// Walks all descendants of the given node via depth-first cursor traversal,
/// looking for language-specific call expression nodes and extracting the
/// callee's simple name. Returns a deduplicated set of names, excluding
/// names present in the provided blocklist.
pub fn extract_calls(
    node: &Node,
    source: &[u8],
    language: &str,
    blocklist: &HashSet<&str>,
) -> HashSet<String> {
    let call_kinds: &[&str] = match language {
        "python" => &["call"],
        "typescript" | "javascript" | "tsx" | "jsx" => &["call_expression", "new_expression"],
        "rust" => &["call_expression"],
        "swift" => &["call_expression"],
        "elixir" => &["call"],
        _ => return HashSet::new(),
    };

    let mut callees = HashSet::new();
    let mut cursor = node.walk();
    let mut reached_root = false;

    loop {
        let n = cursor.node();

        if call_kinds.contains(&n.kind()) {
            if let Some(name) = extract_callee_name(&n, source, language) {
                if !name.is_empty() && !blocklist.contains(name.as_str()) {
                    callees.insert(name);
                }
            }
        }

        // Depth-first traversal via tree-sitter cursor (same pattern as count_complexity).
        if cursor.goto_first_child() {
            continue;
        }
        if cursor.goto_next_sibling() {
            continue;
        }
        loop {
            if !cursor.goto_parent() {
                reached_root = true;
                break;
            }
            if cursor.goto_next_sibling() {
                break;
            }
        }
        if reached_root {
            break;
        }
    }

    callees
}

/// A code entity extracted from a source file via tree-sitter.
///
/// Represents a discrete unit of code (class, function, method, import, etc.)
/// with enough structural metadata to build a navigable knowledge graph.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParsedEntity {
    /// The [`NodeType`] classification (Class, Function, Method, etc.).
    pub kind: NodeType,
    /// Simple name of the entity (e.g. "MyClass", "process").
    pub name: String,
    /// Dot-separated scope path (e.g. "MyModule.MyClass.process").
    pub qualified_name: String,
    /// 0-based first line of the entity in the source file.
    pub start_line: usize,
    /// 0-based last line of the entity in the source file.
    pub end_line: usize,
    /// Type signature or parameter list for functions/methods.
    pub signature: String,
    /// Documentation string if present.
    pub docstring: String,
    /// The raw source text of the entity (capped for storage).
    pub raw_text: String,
    /// Qualified name of the containing entity, if any.
    pub parent_qualified_name: Option<String>,
    /// Base classes/protocols for class-like entities.
    pub bases: Vec<String>,
    /// Import targets for import statements.
    pub imports: Vec<String>,
    /// McCabe cyclomatic complexity for functions/methods (1 + decision points).
    /// `None` for non-function entities (classes, imports, variables, etc.).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cyclomatic_complexity: Option<u32>,
}

impl ParsedEntity {
    /// Generate the text to be embedded for semantic search.
    ///
    /// Different entity kinds emphasize different parts of their
    /// definition to produce the most useful embedding for retrieval.
    pub fn embed_text(&self) -> String {
        match self.kind {
            NodeType::File => format!("file: {}", self.qualified_name),
            NodeType::Class => {
                let doc = if !self.docstring.is_empty() {
                    let truncated = truncate_str(&self.docstring, DOCSTRING_LIMIT);
                    format!(": {truncated}")
                } else {
                    String::new()
                };
                format!("class {}{doc}", self.name)
            }
            NodeType::Function | NodeType::Method => {
                let doc = if !self.docstring.is_empty() {
                    let truncated = truncate_str(&self.docstring, DOCSTRING_LIMIT);
                    format!(": {truncated}")
                } else {
                    String::new()
                };
                format!(
                    "{} {} {}{doc}",
                    self.kind, self.qualified_name, self.signature
                )
            }
            NodeType::Variable | NodeType::Constant => {
                format!("{} {}: {}", self.kind, self.qualified_name, self.signature)
            }
            NodeType::Import => format!("import {}", self.name),
            NodeType::TypeAlias => {
                let truncated = truncate_str(&self.raw_text, RAW_TEXT_SMALL_LIMIT);
                format!("type {}: {truncated}", self.name)
            }
            _ => format!("{} {}", self.kind, self.name),
        }
    }
}

/// A relationship between two parsed entities.
///
/// Source and target are qualified names that will be resolved to
/// node IDs during the ingestion pipeline's edge resolution pass.
/// The relationship type is a free-form string — well-known types like
/// `"contains"` and `"inherits"` are defined as constants in
/// [`scs_core::node_types::RelationshipType`], but any string is accepted.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParsedEdge {
    pub source_qualified_name: String,
    pub target_qualified_name: String,
    pub relationship: String,
    pub weight: f64,
}

impl ParsedEdge {
    /// Create a new edge with default weight of 1.0.
    pub fn new(source: String, target: String, relationship: &str) -> Self {
        Self {
            source_qualified_name: source,
            target_qualified_name: target,
            relationship: relationship.to_string(),
            weight: 1.0,
        }
    }
}

/// Trait for language-specific AST parsers.
///
/// Each implementation uses tree-sitter to parse source code into
/// a concrete syntax tree, then walks the tree to extract entities
/// and relationships.
pub trait LanguageParser: Send + Sync {
    /// Parse source code and extract entities and edges.
    ///
    /// # Arguments
    /// * `source` — The raw source code text.
    /// * `file_path` — Relative path of the file (used for qualified names).
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncate_str_ascii() {
        assert_eq!(truncate_str("hello", 3), "hel");
        assert_eq!(truncate_str("hello", 10), "hello");
        assert_eq!(truncate_str("hello", 5), "hello");
    }

    #[test]
    fn truncate_str_multibyte_boundary() {
        // '─' is 3 bytes (E2 94 80). Truncating at byte 1 or 2 into
        // the character must not panic — it should back up.
        let s = "ab─cd";
        assert_eq!(truncate_str(s, 3), "ab");
        assert_eq!(truncate_str(s, 4), "ab");
        assert_eq!(truncate_str(s, 5), "ab─");
        assert_eq!(truncate_str(s, 6), "ab─c");
    }

    #[test]
    fn truncate_str_emoji() {
        // '🔥' is 4 bytes. Cutting at byte 1–3 must back up.
        let s = "x🔥y";
        assert_eq!(truncate_str(s, 1), "x");
        assert_eq!(truncate_str(s, 2), "x");
        assert_eq!(truncate_str(s, 4), "x");
        assert_eq!(truncate_str(s, 5), "x🔥");
        assert_eq!(truncate_str(s, 6), "x🔥y");
    }

    #[test]
    fn python_parser_handles_multibyte_raw_text() {
        // Regression: the old `&raw[..raw.len().min(1024)]` panicked when
        // byte 1024 landed inside a multi-byte character like '─'.
        let parser = python::PythonParser::new();
        // Build a class whose docstring contains multi-byte chars near the 1024 boundary.
        let filler = "─".repeat(500); // 500 × 3 bytes = 1500 bytes
        let source = format!("class Foo:\n    \"\"\"{filler}\"\"\"\n    pass\n");
        // Should not panic.
        let (entities, _) = parser.parse(&source, "test.py");
        let cls = entities.iter().find(|e| e.name == "Foo").unwrap();
        assert!(cls.raw_text.len() <= RAW_TEXT_LIMIT);
        assert!(cls.raw_text.is_char_boundary(cls.raw_text.len()));
    }
}
