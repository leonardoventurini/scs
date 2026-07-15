//! CSS AST extraction via tree-sitter-css.
//!
//! Extracts class selectors, ID selectors, `@keyframes` animations,
//! CSS custom properties (`--*`), and `@import` statements from
//! stylesheets. CSS has no traditional programming constructs, so
//! entities are mapped to the closest [`NodeType`] equivalents:
//!
//! - `.class-name` → [`NodeType::Class`]
//! - `#id-name` → [`NodeType::Class`] (prefixed with `#`)
//! - `@keyframes name` → [`NodeType::Function`]
//! - `--custom-property` → [`NodeType::Variable`]
//! - `@import` → [`NodeType::Import`]

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{
    truncate_str, LanguageParser, ParsedEdge, ParsedEntity, RAW_TEXT_LIMIT, RAW_TEXT_SMALL_LIMIT,
};

/// Extract the text of a tree-sitter node from the source bytes.
fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

/// Find the first direct child with the given node kind.
fn find_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let result = node.children(&mut cursor).find(|c| c.kind() == kind);
    result
}

pub struct CssParser {}

impl Default for CssParser {
    fn default() -> Self {
        Self::new()
    }
}

impl CssParser {
    pub fn new() -> Self {
        Self {}
    }
}

impl LanguageParser for CssParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let mut parser = Parser::new();
        let language = tree_sitter_css::LANGUAGE;
        parser
            .set_language(&language.into())
            .expect("failed to load css grammar");

        let tree = match parser.parse(source, None) {
            Some(t) => t,
            None => return (vec![], vec![]),
        };

        let source_bytes = source.as_bytes();
        let root = tree.root_node();

        // Derive module qualified name: `src/styles/main.css` → `src.styles.main`
        let module_name = {
            let dotted = file_path.replace('/', ".");
            if let Some(stripped) = dotted.strip_suffix(".css") {
                stripped.to_string()
            } else {
                dotted
            }
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        // Track seen selector names for deduplication — one entity per unique name.
        let mut seen_selectors: HashSet<String> = HashSet::new();

        // File entity.
        let file_raw = truncate_str(source, RAW_TEXT_LIMIT);
        entities.push(ParsedEntity {
            kind: NodeType::File,
            name: file_path.to_string(),
            qualified_name: module_name.clone(),
            start_line: 0,
            end_line: root.end_position().row,
            signature: String::new(),
            docstring: String::new(),
            raw_text: file_raw.to_string(),
            parent_qualified_name: None,
            bases: vec![],
            imports: vec![],
            cyclomatic_complexity: None,
        });

        // Recursive walk — CSS selectors and custom properties nest inside
        // rule_set → block → declaration hierarchies.
        let mut cursor = root.walk();
        let mut reached_root = false;
        loop {
            let node = cursor.node();

            match node.kind() {
                "import_statement" => {
                    self.extract_import(
                        &node,
                        source_bytes,
                        &module_name,
                        &mut entities,
                        &mut edges,
                    );
                }

                "rule_set" => {
                    // Extract class and ID selectors from this rule set.
                    self.extract_selectors(
                        &node,
                        source_bytes,
                        &module_name,
                        &mut entities,
                        &mut edges,
                        &mut seen_selectors,
                    );
                    // Extract custom properties from declarations inside this rule.
                    self.extract_custom_properties(
                        &node,
                        source_bytes,
                        &module_name,
                        &mut entities,
                        &mut edges,
                    );
                }

                "keyframes_statement" => {
                    self.extract_keyframes(
                        &node,
                        source_bytes,
                        &module_name,
                        &mut entities,
                        &mut edges,
                    );
                }

                _ => {}
            }

            // Depth-first traversal — but skip descending into rule_set
            // children since we handle them explicitly above.
            let should_descend = node.kind() != "rule_set";
            if should_descend && cursor.goto_first_child() {
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

        (entities, edges)
    }
}

impl CssParser {
    /// Extract `@import` statement → Import entity.
    fn extract_import(
        &self,
        node: &Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        // The import target is either a string_value or inside a call_expression (url(...)).
        let target = self.get_import_target(node, source);
        if target.is_empty() {
            return;
        }

        let qualified = format!("{module_name}.{target}");

        entities.push(ParsedEntity {
            kind: NodeType::Import,
            name: target.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: String::new(),
            docstring: String::new(),
            raw_text: get_text(node, source).to_string(),
            parent_qualified_name: Some(module_name.to_string()),
            bases: vec![],
            imports: vec![target.clone()],
            cyclomatic_complexity: None,
        });

        edges.push(ParsedEdge::new(module_name.to_string(), target, "imports"));
    }

    /// Resolve the import target path from an `@import` node.
    ///
    /// Handles both `@import "path.css"` and `@import url("path.css")`.
    fn get_import_target(&self, node: &Node, source: &[u8]) -> String {
        // Walk children looking for string_value or call_expression.
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "string_value" => {
                    return get_text(&child, source)
                        .trim_matches('"')
                        .trim_matches('\'')
                        .to_string();
                }
                "call_expression" => {
                    // url("path.css") — find the arguments → string_value inside.
                    if let Some(args) = find_child_by_kind(&child, "arguments") {
                        if let Some(sv) = find_child_by_kind(&args, "string_value") {
                            return get_text(&sv, source)
                                .trim_matches('"')
                                .trim_matches('\'')
                                .to_string();
                        }
                    }
                }
                _ => {}
            }
        }
        String::new()
    }

    /// Extract `.class` and `#id` selectors from a rule_set → Class entities.
    fn extract_selectors(
        &self,
        rule_set: &Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        seen: &mut HashSet<String>,
    ) {
        // The `selectors` child contains the selector list.
        let selectors_node = match find_child_by_kind(rule_set, "selectors") {
            Some(s) => s,
            None => return,
        };

        // Recursively walk all descendants of the selectors node to find
        // class_selector and id_selector nodes (they may be nested in
        // compound selectors, descendant selectors, etc.).
        let mut cursor = selectors_node.walk();
        let mut reached_end = false;
        loop {
            let node = cursor.node();

            match node.kind() {
                "class_selector" => {
                    // The class name is in the `class_name` child (without the dot).
                    if let Some(name_node) = find_child_by_kind(&node, "class_name") {
                        let raw_name = get_text(&name_node, source);
                        let name = format!(".{raw_name}");
                        if !raw_name.is_empty() && seen.insert(name.clone()) {
                            let qualified = format!("{module_name}.{name}");
                            let raw =
                                truncate_str(get_text(rule_set, source), RAW_TEXT_SMALL_LIMIT);
                            entities.push(ParsedEntity {
                                kind: NodeType::Class,
                                name: name.clone(),
                                qualified_name: qualified.clone(),
                                start_line: node.start_position().row,
                                end_line: rule_set.end_position().row,
                                signature: String::new(),
                                docstring: String::new(),
                                raw_text: raw.to_string(),
                                parent_qualified_name: Some(module_name.to_string()),
                                bases: vec![],
                                imports: vec![],
                                cyclomatic_complexity: None,
                            });
                            edges.push(ParsedEdge::new(
                                module_name.to_string(),
                                qualified,
                                "contains",
                            ));
                        }
                    }
                }
                "id_selector" => {
                    // The ID name is in the `id_name` child (without the hash).
                    if let Some(name_node) = find_child_by_kind(&node, "id_name") {
                        let raw_name = get_text(&name_node, source);
                        let name = format!("#{raw_name}");
                        if !raw_name.is_empty() && seen.insert(name.clone()) {
                            let qualified = format!("{module_name}.{name}");
                            let raw =
                                truncate_str(get_text(rule_set, source), RAW_TEXT_SMALL_LIMIT);
                            entities.push(ParsedEntity {
                                kind: NodeType::Class,
                                name: name.clone(),
                                qualified_name: qualified.clone(),
                                start_line: node.start_position().row,
                                end_line: rule_set.end_position().row,
                                signature: String::new(),
                                docstring: String::new(),
                                raw_text: raw.to_string(),
                                parent_qualified_name: Some(module_name.to_string()),
                                bases: vec![],
                                imports: vec![],
                                cyclomatic_complexity: None,
                            });
                            edges.push(ParsedEdge::new(
                                module_name.to_string(),
                                qualified,
                                "contains",
                            ));
                        }
                    }
                }
                _ => {}
            }

            // Depth-first traversal within selectors.
            if cursor.goto_first_child() {
                continue;
            }
            if cursor.goto_next_sibling() {
                continue;
            }
            loop {
                if !cursor.goto_parent() {
                    reached_end = true;
                    break;
                }
                if cursor.goto_next_sibling() {
                    break;
                }
            }
            if reached_end {
                break;
            }
        }
    }

    /// Extract `--custom-property` declarations from a rule_set → Variable entities.
    fn extract_custom_properties(
        &self,
        rule_set: &Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        // Find the block child containing declarations.
        let block = match find_child_by_kind(rule_set, "block") {
            Some(b) => b,
            None => return,
        };

        let mut cursor = block.walk();
        for child in block.children(&mut cursor) {
            if child.kind() != "declaration" {
                continue;
            }

            // Check if the property name starts with `--`.
            let prop_name_node = match find_child_by_kind(&child, "property_name") {
                Some(n) => n,
                None => continue,
            };

            let prop_name = get_text(&prop_name_node, source);
            if !prop_name.starts_with("--") {
                continue;
            }

            // Extract the value (everything after the colon, before the semicolon).
            let value = self.get_declaration_value(&child, source);
            let qualified = format!("{module_name}.{prop_name}");
            let raw = truncate_str(get_text(&child, source), RAW_TEXT_SMALL_LIMIT);

            entities.push(ParsedEntity {
                kind: NodeType::Variable,
                name: prop_name.to_string(),
                qualified_name: qualified.clone(),
                start_line: child.start_position().row,
                end_line: child.end_position().row,
                signature: value,
                docstring: String::new(),
                raw_text: raw.to_string(),
                parent_qualified_name: Some(module_name.to_string()),
                bases: vec![],
                imports: vec![],
                cyclomatic_complexity: None,
            });

            edges.push(ParsedEdge::new(
                module_name.to_string(),
                qualified,
                "contains",
            ));
        }
    }

    /// Get the value side of a CSS declaration (text after the property name).
    fn get_declaration_value(&self, declaration: &Node, source: &[u8]) -> String {
        // The value is typically everything after property_name and `:`.
        // We collect text from all children that aren't the property_name.
        let full = get_text(declaration, source);
        if let Some(colon_pos) = full.find(':') {
            let value = full[colon_pos + 1..].trim().trim_end_matches(';').trim();
            return value.to_string();
        }
        String::new()
    }

    /// Extract `@keyframes name { ... }` → Function entity.
    fn extract_keyframes(
        &self,
        node: &Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        // The keyframe name is in a `keyframes_name` or `name` child.
        let name = self.get_keyframes_name(node, source);
        if name.is_empty() {
            return;
        }

        let qualified = format!("{module_name}.{name}");
        let raw = truncate_str(get_text(node, source), RAW_TEXT_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Function,
            name: name.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: String::new(),
            docstring: String::new(),
            raw_text: raw.to_string(),
            parent_qualified_name: Some(module_name.to_string()),
            bases: vec![],
            imports: vec![],
            cyclomatic_complexity: None,
        });

        edges.push(ParsedEdge::new(
            module_name.to_string(),
            qualified,
            "contains",
        ));
    }

    /// Extract the name from a `@keyframes` statement.
    fn get_keyframes_name(&self, node: &Node, source: &[u8]) -> String {
        // Try `keyframes_name` child first, then fall back to scanning children.
        if let Some(name_node) = find_child_by_kind(node, "keyframes_name") {
            return get_text(&name_node, source).to_string();
        }
        // Some tree-sitter-css versions use a plain identifier after @keyframes.
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            let kind = child.kind();
            if kind != "@keyframes" && kind != "keyframe_block_list" && kind != "{" && kind != "}" {
                let text = get_text(&child, source).trim().to_string();
                if !text.is_empty() {
                    return text;
                }
            }
        }
        String::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(source: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let parser = CssParser::new();
        parser.parse(source, "src/styles/main.css")
    }

    #[test]
    fn file_entity_created() {
        let (entities, _) = parse("/* empty stylesheet */\n");
        let file = entities.iter().find(|e| e.kind == NodeType::File).unwrap();
        assert_eq!(file.qualified_name, "src.styles.main");
        assert_eq!(file.name, "src/styles/main.css");
    }

    #[test]
    fn parses_class_selector() {
        let source = ".btn { color: red; }\n";
        let (entities, edges) = parse(source);

        let cls = entities
            .iter()
            .find(|e| e.kind == NodeType::Class && e.name == ".btn")
            .expect("should find .btn class entity");

        assert_eq!(cls.qualified_name, "src.styles.main..btn");
        assert!(cls.cyclomatic_complexity.is_none());

        // contains edge
        let edge = edges
            .iter()
            .find(|e| e.relationship == "contains" && e.target_qualified_name.contains(".btn"))
            .expect("should have contains edge for .btn");
        assert_eq!(edge.source_qualified_name, "src.styles.main");
    }

    #[test]
    fn parses_id_selector() {
        let source = "#header { display: flex; }\n";
        let (entities, _) = parse(source);

        let id = entities
            .iter()
            .find(|e| e.kind == NodeType::Class && e.name == "#header")
            .expect("should find #header class entity");

        assert_eq!(id.qualified_name, "src.styles.main.#header");
    }

    #[test]
    fn parses_import() {
        let source = "@import \"reset.css\";\n";
        let (entities, edges) = parse(source);

        let imp = entities
            .iter()
            .find(|e| e.kind == NodeType::Import)
            .expect("should find import entity");

        assert_eq!(imp.name, "reset.css");
        assert_eq!(imp.imports, vec!["reset.css"]);

        let edge = edges
            .iter()
            .find(|e| e.relationship == "imports")
            .expect("should have imports edge");
        assert_eq!(edge.target_qualified_name, "reset.css");
    }

    #[test]
    fn parses_keyframes() {
        let source = "@keyframes fade {\n  from { opacity: 0; }\n  to { opacity: 1; }\n}\n";
        let (entities, edges) = parse(source);

        let kf = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "fade")
            .expect("should find fade keyframes entity");

        assert_eq!(kf.qualified_name, "src.styles.main.fade");
        assert!(kf.cyclomatic_complexity.is_none());

        let edge = edges
            .iter()
            .find(|e| e.relationship == "contains" && e.target_qualified_name.contains("fade"))
            .expect("should have contains edge for keyframes");
        assert_eq!(edge.source_qualified_name, "src.styles.main");
    }

    #[test]
    fn parses_custom_property() {
        let source = ":root {\n  --primary-color: #3490dc;\n  --spacing: 8px;\n}\n";
        let (entities, _) = parse(source);

        let vars: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Variable)
            .collect();

        assert_eq!(vars.len(), 2, "should find 2 custom properties");
        assert!(vars.iter().any(|v| v.name == "--primary-color"));
        assert!(vars.iter().any(|v| v.name == "--spacing"));

        let primary = vars.iter().find(|v| v.name == "--primary-color").unwrap();
        assert_eq!(primary.signature, "#3490dc");
    }

    #[test]
    fn deduplicates_selectors() {
        let source = ".btn { color: red; }\n.btn { font-size: 14px; }\n";
        let (entities, _) = parse(source);

        let btn_count = entities
            .iter()
            .filter(|e| e.kind == NodeType::Class && e.name == ".btn")
            .count();

        assert_eq!(
            btn_count, 1,
            "duplicate .btn should be deduplicated to one entity"
        );
    }

    #[test]
    fn nested_custom_property() {
        let source = ".card {\n  --card-bg: white;\n  background: var(--card-bg);\n}\n";
        let (entities, _) = parse(source);

        let var = entities
            .iter()
            .find(|e| e.kind == NodeType::Variable && e.name == "--card-bg")
            .expect("should find --card-bg custom property inside .card rule");

        assert_eq!(var.signature, "white");
    }
}
