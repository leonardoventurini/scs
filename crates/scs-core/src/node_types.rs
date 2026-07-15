//! Code and provenance discriminators stored by SCS.

use serde::{Deserialize, Serialize};
use strum::{Display, EnumIter, EnumString};

/// A repository-derived code or provenance entity.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Display, EnumString, EnumIter,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum NodeType {
    /// A source file tracked by the indexing pipeline.
    File,
    /// A language-level module or top-level namespace.
    Module,
    /// A named type such as a class, struct, enum, trait, or interface.
    Class,
    /// A top-level callable definition.
    Function,
    /// A callable nested in a named type.
    Method,
    /// A module-level variable.
    Variable,
    /// A module-level constant.
    Constant,
    /// An imported symbol or module.
    Import,
    /// A language-level type alias.
    TypeAlias,
    /// A source-control commit.
    Commit,
    /// A source-control contributor.
    Contributor,
}

/// A repository-derived structural or provenance relationship.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Display, EnumString, EnumIter,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum RelationshipType {
    /// A parent scope contains a child entity.
    Contains,
    /// A callable invokes another callable.
    Calls,
    /// A module imports another module or symbol.
    Imports,
    /// A type inherits from another type.
    Inherits,
    /// A type implements a trait, protocol, or interface.
    Implements,
    /// A code entity refers to another code entity.
    References,
    /// A commit was authored by a contributor.
    AuthoredBy,
    /// A commit modifies a source file.
    Modifies,
}

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use strum::IntoEnumIterator;

    use super::*;

    #[test]
    fn node_types_are_exactly_the_code_only_contract() {
        let actual: Vec<String> = NodeType::iter().map(|value| value.to_string()).collect();
        assert_eq!(
            actual,
            [
                "file",
                "module",
                "class",
                "function",
                "method",
                "variable",
                "constant",
                "import",
                "type_alias",
                "commit",
                "contributor",
            ]
        );
        assert_eq!(NodeType::from_str("type_alias"), Ok(NodeType::TypeAlias));
        assert!(NodeType::from_str("correction").is_err());
        assert!(NodeType::from_str("document").is_err());
    }

    #[test]
    fn relationship_types_are_exactly_the_code_only_contract() {
        let actual: Vec<String> = RelationshipType::iter()
            .map(|value| value.to_string())
            .collect();
        assert_eq!(
            actual,
            [
                "contains",
                "calls",
                "imports",
                "inherits",
                "implements",
                "references",
                "authored_by",
                "modifies",
            ]
        );
        assert_eq!(
            RelationshipType::from_str("authored_by"),
            Ok(RelationshipType::AuthoredBy)
        );
        assert!(RelationshipType::from_str("related_to").is_err());
        assert!(RelationshipType::from_str("corrects").is_err());
    }
}
