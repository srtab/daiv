import datetime

from neomodel import (
    DateTimeProperty,
    JSONProperty,
    One,
    RelationshipFrom,
    RelationshipTo,
    StringProperty,
    StructuredNode,
    StructuredRel,
    UniqueIdProperty,
    ZeroOrOne,
)


class RelationType:
    INCLUDED_IN = "INCLUDED_IN"
    DECLARED_AT = "DECLARED_AT"
    REFERS_CALL = "REFERS_CALL"
    REFERS_CLASS = "REFERS_CLASS"
    IMPLEMENTS = "IMPLEMENTS"
    HAS_METHOD = "HAS_METHOD"


class Position(JSONProperty):
    """
    Represents a position in source code (line, column)
    """

    def __init__(self, **kwargs):
        super().__init__(default={"line": 0, "column": 0}, **kwargs)


class DefinesRel(StructuredRel):
    start_position = Position()
    end_position = Position()
    tag = StringProperty(default="@definition")


class RefersRel(StructuredRel):
    start_position = Position()
    end_position = Position()
    tag = StringProperty(default="@reference")


class BaseNode(StructuredNode):
    __abstract_node__ = True

    uid = UniqueIdProperty()
    created_at = DateTimeProperty(default=datetime.datetime.now)
    updated_at = DateTimeProperty(default=datetime.datetime.now)


class Repository(BaseNode):
    repo_id = StringProperty(unique_index=True)
    name = StringProperty(required=True)
    url = StringProperty()
    client = StringProperty(required=True)

    # Relationships
    folders = RelationshipFrom("Folder", RelationType.INCLUDED_IN)


class Folder(BaseNode):
    path = StringProperty(required=True, unique_index=True)
    name = StringProperty(required=True)

    # Relationships
    repository = RelationshipTo("Repository", RelationType.INCLUDED_IN, cardinality=One)
    parent = RelationshipTo("Folder", RelationType.INCLUDED_IN, cardinality=ZeroOrOne)
    files = RelationshipFrom("File", RelationType.INCLUDED_IN)


class File(BaseNode):
    path = StringProperty(required=True, unique_index=True)
    name = StringProperty(required=True)
    extension = StringProperty()

    # Define relationships to code elements
    folder = RelationshipTo("Folder", RelationType.INCLUDED_IN, cardinality=ZeroOrOne)

    defines_class = RelationshipTo("ClassElement", RelationType.DECLARED_AT, model=DefinesRel)
    defines_function = RelationshipTo("FunctionElement", RelationType.DECLARED_AT, model=DefinesRel)
    defines_interface = RelationshipTo("InterfaceElement", RelationType.DECLARED_AT, model=DefinesRel)
    defines_module = RelationshipTo("ModuleElement", RelationType.DECLARED_AT, model=DefinesRel)

    refers_call = RelationshipTo("FunctionElement", RelationType.REFERS_CALL, model=RefersRel)
    refers_class = RelationshipTo("ClassElement", RelationType.REFERS_CLASS, model=RefersRel)
    refers_implementation = RelationshipTo("InterfaceElement", RelationType.IMPLEMENTS, model=RefersRel)


class CodeElement(BaseNode):
    __abstract_node__ = True

    name = StringProperty(required=True)

    # Relationships
    defined_in = RelationshipFrom("File", RelationType.DECLARED_AT, model=DefinesRel)


class ClassElement(CodeElement):
    __label__ = "Class"

    superclasses = StringProperty()

    # Relationships
    methods = RelationshipTo("MethodElement", RelationType.DECLARED_AT)
    implements = RelationshipTo("InterfaceElement", RelationType.IMPLEMENTS, model=RefersRel)
    referenced_by = RelationshipFrom("File", RelationType.REFERS_CLASS, model=RefersRel)


class FunctionElement(CodeElement):
    __label__ = "Function"

    parameters = StringProperty()
    return_type = StringProperty()

    # Relationships
    called_by = RelationshipFrom("File", RelationType.REFERS_CALL, model=RefersRel)


class InterfaceElement(CodeElement):
    __label__ = "Interface"

    # Relationships
    implemented_by = RelationshipFrom("ClassElement", RelationType.IMPLEMENTS, model=RefersRel)
    implementation_referred_by = RelationshipFrom("File", RelationType.IMPLEMENTS, model=RefersRel)


class MethodElement(CodeElement):
    __label__ = "Method"

    parameters = StringProperty()
    return_type = StringProperty()

    # Relationships
    class_owner = RelationshipFrom("ClassElement", RelationType.HAS_METHOD)
    called_by = RelationshipFrom("File", RelationType.REFERS_CALL, model=RefersRel)


class ModuleElement(CodeElement):
    __label__ = "Module"
