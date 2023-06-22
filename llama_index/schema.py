"""Base schema for data structures."""
import uuid
from abc import abstractmethod
from enum import Enum, auto
from hashlib import sha256
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Union


DEFAULT_TEXT_NODE_TMPL = "{metadata_str}\n\n{content}"
DEFAULT_METADATA_TMPL = "{key}: {value}"


def _validate_is_flat_dict(metadata_dict: dict) -> None:
    """
    Validate that metadata dict is flat,
    and key is str, and value is one of (str, int, float, None).
    """
    for key, val in metadata_dict.items():
        if not isinstance(key, str):
            raise ValueError("Metadata key must be str!")
        if not isinstance(val, (str, int, float, type(None))):
            raise ValueError("Value must be one of (str, int, float, None)")


class NodeRelationship(str, Enum):
    """Node relationships used in `BaseNode` class.

    Attributes:
        SOURCE: The node is the source document.
        PREVIOUS: The node is the previous node in the document.
        NEXT: The node is the next node in the document.
        PARENT: The node is the parent node in the document.
        CHILD: The node is a child node in the document.

    """

    SOURCE = auto()
    PREVIOUS = auto()
    NEXT = auto()
    PARENT = auto()
    CHILD = auto()


class ObjectType(str, Enum):
    DOCUMENT = auto()
    TEXT = auto()
    IMAGE = auto()
    INDEX = auto()


class RelatedNodeInfo(BaseModel):
    node_id: str
    node_type: Optional[ObjectType] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    hash: Optional[str] = None


RelatedNodeType = Union[RelatedNodeInfo, List[RelatedNodeInfo]]


class BaseNode(BaseModel):
    """Base node Object.

    Generic abstract interface for retrievable nodes

    """

    id_: str = Field(
        default_factory=lambda: str(uuid.uuid4()), description="Unique ID of the node."
    )
    embedding: Optional[List[float]] = Field(
        default=None, description="Embedding of the node."
    )
    hash: str = Field(default="", description="Hash of the node content.")
    weight: float = Field(
        default=1.0,
        description="Optional field to weight the node similarity during retrieval.",
    )

    """"
    metadata fields
    - injected as part of the text shown to LLMs as context
    - used by vector DBs for metadata filtering

    This must be a flat dictionary, 
    and only uses str keys, and (str, int, float) values.
    """
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="A flat dictionary of metadata fields"
    )
    usable_metadata: Optional[List[str]] = Field(
        default=None,
        description="Metadata keys that are used during retrieval.",
    )
    relationships: Dict[NodeRelationship, RelatedNodeType] = Field(
        default_factory=dict,
        description="A mapping of relationships to other node information.",
    )

    def __post_init__(self) -> None:
        """Post init."""
        # assign hash if not set
        if not self.hash:
            self.hash = self._generate_node_hash()

        if self.metadata is not None:
            _validate_is_flat_dict(self.metadata)

    @abstractmethod
    def _generate_node_hash(self) -> str:
        """Generate a hash to represent the node."""

    @classmethod
    @abstractmethod
    def get_type(cls) -> str:
        """Get Object type."""

    @abstractmethod
    def get_content(self) -> str:
        """Get object content."""

    @abstractmethod
    def metadata_str(self) -> str:
        """Extra info string."""

    @abstractmethod
    def set_content(self, value: Any) -> None:
        """Set the content of the node."""

    @property
    def node_id(self) -> str:
        return self.id_

    # TODO: DEPRECATED
    def get_doc_id(self) -> str:
        return self.id_

    @property
    def source_node(self) -> Optional[RelatedNodeInfo]:
        """Source object node.

        Extracted from the relationships field.

        """
        if NodeRelationship.SOURCE not in self.relationships:
            return None

        relation = self.relationships[NodeRelationship.SOURCE]
        if isinstance(relation, list):
            raise ValueError("Source object must be a single RelatedNodeInfo object")
        return relation

    @property
    def prev_node(self) -> Optional[RelatedNodeInfo]:
        """Prev node."""
        if NodeRelationship.PREVIOUS not in self.relationships:
            return None

        relation = self.relationships[NodeRelationship.PREVIOUS]
        if not isinstance(relation, RelatedNodeInfo):
            raise ValueError("Previous object must be a single RelatedNodeInfo object")
        return relation

    @property
    def next_node(self) -> Optional[RelatedNodeInfo]:
        """Next node."""
        if NodeRelationship.NEXT not in self.relationships:
            return None

        relation = self.relationships[NodeRelationship.NEXT]
        if not isinstance(relation, RelatedNodeInfo):
            raise ValueError("Next object must be a single RelatedNodeInfo object")
        return relation

    @property
    def parent_node(self) -> Optional[RelatedNodeInfo]:
        """Parent node."""
        if NodeRelationship.PARENT not in self.relationships:
            return None

        relation = self.relationships[NodeRelationship.PARENT]
        if not isinstance(relation, RelatedNodeInfo):
            raise ValueError("Parent object must be a single RelatedNodeInfo object")
        return relation

    @property
    def child_nodes(self) -> Optional[List[RelatedNodeInfo]]:
        """Child nodes."""
        if NodeRelationship.CHILD not in self.relationships:
            return None

        relation = self.relationships[NodeRelationship.PARENT]
        if not isinstance(relation, list):
            raise ValueError("Child objects must be a list of RelatedNodeInfo objects.")
        return relation

    @property
    def ref_doc_id(self) -> Optional[str]:
        """Deprecated: Get ref doc id."""
        source_node = self.source_node
        if source_node is None:
            return None
        return source_node.node_id

    @property
    def extra_info(self) -> Dict[str, Any]:
        """TODO: DEPRECATED: Extra info."""
        return self.metadata

    def get_embedding(self) -> List[float]:
        """Get embedding.

        Errors if embedding is None.

        """
        if self.embedding is None:
            raise ValueError("embedding not set.")
        return self.embedding

    def as_related_node_info(self) -> RelatedNodeInfo:
        """Get node as RelatedNodeInfo."""
        return RelatedNodeInfo(
            node_id=self.node_id, metadata=self.metadata, hash=self.hash
        )


class TextNode(BaseNode):
    text: str = Field(default="", description="Text content of the node.")
    start_char_idx: Optional[int] = Field(
        default=None, description="Start char index of the node."
    )
    end_char_idx: Optional[int] = Field(
        default=None, description="End char index of the node."
    )
    text_template: str = Field(
        default=DEFAULT_TEXT_NODE_TMPL,
        description=(
            "Template for how text is formatted, with {content} and "
            "{metadata_str} placeholders."
        ),
    )
    metadata_template: str = Field(
        default=DEFAULT_METADATA_TMPL,
        description=(
            "Template for how metadata is formatted, with {key} and "
            "{value} placeholders."
        ),
    )
    metadata_seperator: str = Field(
        default="\n",
        description="Seperator between metadata fields when converting to string.",
    )

    def _generate_node_hash(self) -> str:
        """Generate a hash to represent the node."""
        doc_identity = str(self.text) + str(self.metadata_str)
        return sha256(doc_identity.encode("utf-8", "surrogatepass")).hexdigest()

    @classmethod
    def get_type(cls) -> str:
        """Get Object type."""
        return ObjectType.TEXT

    def get_content(self) -> str:
        """Get object content."""
        metadata_str = self.metadata_str()
        return self.text_template.format(content=self.text, metadata_str=metadata_str)

    def metadata_str(self) -> str:
        """Convert metadata to a string."""

        return self.metadata_seperator.join(
            [
                self.metadata_template.format(key=key, value=str(value))
                for key, value in self.metadata.items()
                if self.usable_metadata is None or key in self.usable_metadata
            ]
        )

    def set_content(self, value: str) -> None:
        """Set the content of the node."""
        self.text = value

    def get_node_info(self) -> Dict[str, Any]:
        """Get node info."""
        return {"start": self.start_char_idx, "end": self.end_char_idx}

    # TODO: deprecated node properties
    def get_text(self) -> str:
        """Deprecated: Get text."""
        return self.get_content()

    @property
    def node_info(self) -> Dict[str, Any]:
        """Deprecated: Get node info."""
        return self.get_node_info()


class ImageNode(TextNode):
    """Node with image."""

    # TODO: store reference instead of actual image
    # base64 encoded image str
    image: str

    @classmethod
    def get_type(cls) -> str:
        return ObjectType.IMAGE


class IndexNode(TextNode):
    """Node with reference to an index."""

    index_id: str

    @classmethod
    def get_type(cls) -> str:
        return ObjectType.INDEX


class NodeWithScore(BaseModel):
    node: BaseNode
    score: Optional[float] = None
